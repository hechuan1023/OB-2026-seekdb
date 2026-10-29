/* seekdb <-> hipVS bridge: plain-C CAGRA kNN; all cuVS/ROCm deps live here. */
#include "seekdb_cuvs_bridge.h"
#include <cuvs/core/c_api.h>
#include <cuvs/neighbors/cagra.h>
#include <dlpack/dlpack.h>
#ifdef __HIP_PLATFORM_AMD__
#include <cuvs/cuda_runtime.h>
#else
#include <cuda_runtime.h>
#endif
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <stdint.h>

typedef struct {
  cuvsResources_t        res;
  cuvsCagraIndexParams_t ip;
  cuvsCagraIndex_t       idx;
  long n, dim;
} sk_cuvs_index;

/* ---------------------------------------------------------------------------
 * Run-time tunable CAGRA build/search parameters.
 *
 * The frozen V5 baseline uses graph_degree=128, intermediate_graph_degree=256
 * and nn_descent_niter=100. Development comparisons at 250K vectors in both
 * 128 and 256 dimensions support a 192-degree graph built from a 192-degree
 * intermediate graph with 25 NN-Descent iterations. Search defaults are retained.
 * Formal contest quality and resource limits are verified by CI.
 *
 * The parameters are therefore resolved from the environment at run time, so one
 * compiled artefact can be A/B tested on the AMD cloud instance without a rebuild.
 * Without overrides the candidate uses 192/192/25. The explicit baseline
 * profile restores 128/256/100 for development comparisons. This is the same
 * selection for every build, independent of dataset identity or testing phase.
 *
 *   OB_CUVS_PROFILE                   baseline | fast | aggressive (optional)
 *   OB_CUVS_BUILD_ALGO                nn_descent (default) | iterative_search
 *   OB_CUVS_GRAPH_DEGREE              default 192
 *   OB_CUVS_INTERMEDIATE_GRAPH_DEGREE default 192
 *   OB_CUVS_NN_DESCENT_NITER          default 25
 *   OB_CUVS_ITOPK_SIZE                default: keep the library default
 *   OB_CUVS_SEARCH_WIDTH              default 4
 *
 * Any explicit OB_CUVS_* variable wins over the profile.
 * ------------------------------------------------------------------------- */

typedef struct {
  int   build_algo;                 /* NN_DESCENT / ITERATIVE_CAGRA_SEARCH */
  long  graph_degree;
  long  intermediate_graph_degree;
  long  nn_descent_niter;
  long  itopk_size;                 /* <= 0: do not touch the library default */
  long  search_width;
} sk_cuvs_params;

static long sk_env_long(const char* name, long dflt, long lo, long hi)
{
  const char* v = getenv(name);
  if (v == NULL || v[0] == '\0') { return dflt; }
  char* end = NULL;
  long r = strtol(v, &end, 10);
  if (end == v) { return dflt; }
  if (r < lo) { r = lo; }
  if (r > hi) { r = hi; }
  return r;
}

static void sk_resolve_params(sk_cuvs_params* p)
{
  /* Retain more final edges while reducing intermediate work; use FP32 throughout. */
  p->build_algo = NN_DESCENT;
  p->graph_degree = 192;
  p->intermediate_graph_degree = 192;
  p->nn_descent_niter = 25;
  p->itopk_size = 0;
  p->search_width = 4;

  const char* profile = getenv("OB_CUVS_PROFILE");
  if (profile != NULL && profile[0] != '\0') {
    if (strcmp(profile, "baseline") == 0) {
      p->graph_degree = 128;
      p->intermediate_graph_degree = 256;
      p->nn_descent_niter = 100;
    } else if (strcmp(profile, "fast") == 0) {
      p->graph_degree = 64;
      p->intermediate_graph_degree = 128;
      p->nn_descent_niter = 30;
    } else if (strcmp(profile, "aggressive") == 0) {
      p->graph_degree = 64;
      p->intermediate_graph_degree = 128;
      p->nn_descent_niter = 20;
    }
  }

  const char* algo = getenv("OB_CUVS_BUILD_ALGO");
  if (algo != NULL && algo[0] != '\0') {
    if (strcmp(algo, "iterative_search") == 0) { p->build_algo = ITERATIVE_CAGRA_SEARCH; }
    else if (strcmp(algo, "nn_descent") == 0) { p->build_algo = NN_DESCENT; }
  }

  p->graph_degree = sk_env_long("OB_CUVS_GRAPH_DEGREE", p->graph_degree, 8, 512);
  p->intermediate_graph_degree =
      sk_env_long("OB_CUVS_INTERMEDIATE_GRAPH_DEGREE", p->intermediate_graph_degree, 8, 512);
  p->nn_descent_niter = sk_env_long("OB_CUVS_NN_DESCENT_NITER", p->nn_descent_niter, 1, 1000);
  p->itopk_size = sk_env_long("OB_CUVS_ITOPK_SIZE", p->itopk_size, 0, 4096);
  p->search_width = sk_env_long("OB_CUVS_SEARCH_WIDTH", p->search_width, 1, 64);
}

static double sk_now_sec()
{
  struct timespec ts;
  if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) { return 0.0; }
  return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

void* seekdb_cuvs_build(const float* base, long n, long dim)
{
  sk_cuvs_index* h = (sk_cuvs_index*)calloc(1, sizeof(*h));
  if (!h) return NULL;
  if (cuvsResourcesCreate(&h->res) != CUVS_SUCCESS) { free(h); return NULL; }

  DLManagedTensor dset; long ds[2] = {n, dim};
  dset.dl_tensor.data = (void*)base;
  dset.dl_tensor.device.device_type = kDLCPU; dset.dl_tensor.device.device_id = 0;
  dset.dl_tensor.ndim = 2;
  dset.dl_tensor.dtype.code = kDLFloat; dset.dl_tensor.dtype.bits = 32; dset.dl_tensor.dtype.lanes = 1;
  dset.dl_tensor.shape = ds; dset.dl_tensor.strides = 0; dset.dl_tensor.byte_offset = 0;

  sk_cuvs_params p;
  sk_resolve_params(&p);

  if (cuvsCagraIndexParamsCreate(&h->ip) != CUVS_SUCCESS) goto fail_res;
  h->ip->build_algo = p.build_algo;
  h->ip->graph_degree = (int)p.graph_degree;
  h->ip->intermediate_graph_degree = (int)p.intermediate_graph_degree;
  h->ip->nn_descent_niter = (int)p.nn_descent_niter;
  if (cuvsCagraIndexCreate(&h->idx) != CUVS_SUCCESS) goto fail_ip;
  {
    const double t0 = sk_now_sec();
    if (cuvsCagraBuild(h->res, h->ip, &dset, h->idx) != CUVS_SUCCESS) goto fail_idx;
    fprintf(stderr,
            "[seekdb_cuvs] build done n=%ld dim=%ld algo=%d degree=%ld inter=%ld niter=%ld "
            "itopk=%ld elapsed=%.3fs\n",
            n, dim, (int)p.build_algo, p.graph_degree, p.intermediate_graph_degree,
            p.nn_descent_niter, p.itopk_size, sk_now_sec() - t0);
  }
  h->n = n; h->dim = dim;
  return h;

fail_idx: cuvsCagraIndexDestroy(h->idx);
fail_ip:  cuvsCagraIndexParamsDestroy(h->ip);
fail_res: cuvsResourcesDestroy(h->res); free(h);
  fprintf(stderr, "[seekdb_cuvs] build failed: %s\n", cuvsGetLastErrorText());
  return NULL;
}

int seekdb_cuvs_search(void* handle, const float* query, long nq, long topk,
                       unsigned int* out_ids, float* out_dist)
{
  sk_cuvs_index* h = (sk_cuvs_index*)handle;
  if (!h) return 1;
  int rc = 0;
  float* qd = 0; unsigned* nd = 0; float* dd = 0; cuvsCagraSearchParams_t sp = 0;
  sk_cuvs_params p;
  sk_resolve_params(&p);
  if (cuvsRMMAlloc(h->res, (void**)&qd, sizeof(float)*nq*h->dim)  != CUVS_SUCCESS) { rc=10; goto done; }
  if (cuvsRMMAlloc(h->res, (void**)&nd, sizeof(unsigned)*nq*topk) != CUVS_SUCCESS) { rc=11; goto done; }
  if (cuvsRMMAlloc(h->res, (void**)&dd, sizeof(float)*nq*topk)    != CUVS_SUCCESS) { rc=12; goto done; }
  if (cudaMemcpy(qd, query, sizeof(float)*nq*h->dim, cudaMemcpyDefault) != cudaSuccess) { rc=13; goto done; }
  {
    DLManagedTensor qt, nt, dt; long qs[2]={nq,h->dim}, nsh[2]={nq,topk}, dsh[2]={nq,topk};
    qt.dl_tensor.data=qd; qt.dl_tensor.device.device_type=kDLCUDA; qt.dl_tensor.device.device_id=0;
    qt.dl_tensor.ndim=2; qt.dl_tensor.dtype.code=kDLFloat; qt.dl_tensor.dtype.bits=32; qt.dl_tensor.dtype.lanes=1;
    qt.dl_tensor.shape=qs; qt.dl_tensor.strides=0; qt.dl_tensor.byte_offset=0;
    nt=qt; nt.dl_tensor.data=nd; nt.dl_tensor.dtype.code=kDLUInt; nt.dl_tensor.shape=nsh;
    dt=qt; dt.dl_tensor.data=dd; dt.dl_tensor.dtype.code=kDLFloat; dt.dl_tensor.shape=dsh;
    if (cuvsCagraSearchParamsCreate(&sp) != CUVS_SUCCESS) { rc=14; goto done; }
    {
      size_t required_itopk = (size_t)topk * 4;
      if (required_itopk < 512) required_itopk = 512;
      if (sp->itopk_size < required_itopk) sp->itopk_size = required_itopk;
      if (p.itopk_size > 0) { sp->itopk_size = (size_t)p.itopk_size; }
      sp->search_width = (int)p.search_width;
    }
    cuvsFilter f; f.type=NO_FILTER; f.addr=(uintptr_t)0;
    if (cuvsCagraSearch(h->res, sp, h->idx, &qt, &nt, &dt, f) != CUVS_SUCCESS) { rc=15; goto done; }
    if (cudaMemcpy(out_ids, nd, sizeof(unsigned)*nq*topk, cudaMemcpyDefault) != cudaSuccess) { rc=16; goto done; }
    if (out_dist) cudaMemcpy(out_dist, dd, sizeof(float)*nq*topk, cudaMemcpyDefault);
  }
done:
  if (sp) cuvsCagraSearchParamsDestroy(sp);
  if (dd) cuvsRMMFree(h->res, dd, sizeof(float)*nq*topk);
  if (nd) cuvsRMMFree(h->res, nd, sizeof(unsigned)*nq*topk);
  if (qd) cuvsRMMFree(h->res, qd, sizeof(float)*nq*h->dim);
  if (rc) fprintf(stderr, "[seekdb_cuvs] search rc=%d: %s\n", rc, cuvsGetLastErrorText());
  return rc;
}

void seekdb_cuvs_free(void* handle)
{
  sk_cuvs_index* h = (sk_cuvs_index*)handle;
  if (!h) return;
  cuvsCagraIndexDestroy(h->idx);
  cuvsCagraIndexParamsDestroy(h->ip);
  cuvsResourcesDestroy(h->res);
  free(h);
}

int seekdb_cuvs_serialize(void* handle, const char* filename)
{
  sk_cuvs_index* index = (sk_cuvs_index*)handle;
  if (!index || !filename) return 1;
  if (cuvsCagraSerialize(index->res, filename, index->idx, true) != CUVS_SUCCESS) {
    fprintf(stderr, "[seekdb_cuvs] serialize failed: %s\n", cuvsGetLastErrorText());
    return 1;
  }
  return 0;
}

void* seekdb_cuvs_deserialize(const char* filename, long n, long dim)
{
  if (!filename || n <= 0 || dim <= 0) return NULL;
  sk_cuvs_index* index = (sk_cuvs_index*)calloc(1, sizeof(*index));
  if (!index) return NULL;
  if (cuvsResourcesCreate(&index->res) != CUVS_SUCCESS) goto fail;
  if (cuvsCagraIndexParamsCreate(&index->ip) != CUVS_SUCCESS) goto fail_res;
  if (cuvsCagraIndexCreate(&index->idx) != CUVS_SUCCESS) goto fail_ip;
  if (cuvsCagraDeserialize(index->res, filename, index->idx) != CUVS_SUCCESS) goto fail_idx;
  {
    int64_t actual_n = 0;
    int64_t actual_dim = 0;
    if (cuvsCagraIndexGetSize(index->idx, &actual_n) != CUVS_SUCCESS ||
        cuvsCagraIndexGetDims(index->idx, &actual_dim) != CUVS_SUCCESS ||
        actual_n != n || actual_dim != dim) {
      fprintf(stderr, "[seekdb_cuvs] snapshot shape mismatch: %ld x %ld, expected %ld x %ld\n",
              (long)actual_n, (long)actual_dim, n, dim);
      goto fail_idx;
    }
  }
  index->n = n;
  index->dim = dim;
  return index;
fail_idx:
  cuvsCagraIndexDestroy(index->idx);
fail_ip:
  cuvsCagraIndexParamsDestroy(index->ip);
fail_res:
  cuvsResourcesDestroy(index->res);
fail:
  fprintf(stderr, "[seekdb_cuvs] deserialize failed: %s\n", cuvsGetLastErrorText());
  free(index);
  return NULL;
}

int seekdb_cuvs_copy_dataset(void* handle, float* output, long n, long dim)
{
  sk_cuvs_index* index = (sk_cuvs_index*)handle;
  if (!index || !output || n <= 0 || dim <= 0 || n != index->n || dim != index->dim ||
      (size_t)n > SIZE_MAX / sizeof(float) / (size_t)dim) return 1;
  DLManagedTensor dataset = {0};
  if (cuvsCagraIndexGetDataset(index->idx, &dataset) != CUVS_SUCCESS) return 1;
  const DLTensor* tensor = &dataset.dl_tensor;
  int rc = 1;
  if (tensor->data && tensor->ndim == 2 && tensor->shape &&
      tensor->shape[0] == n && tensor->shape[1] == dim &&
      tensor->dtype.code == kDLFloat && tensor->dtype.bits == 32 && tensor->dtype.lanes == 1 &&
      tensor->device.device_type == kDLCUDA &&
      (!tensor->strides || (tensor->strides[1] == 1 && tensor->strides[0] >= dim))) {
    const size_t width = sizeof(float) * (size_t)dim;
    const uint64_t stride = tensor->strides ? (uint64_t)tensor->strides[0] : (uint64_t)dim;
    if (stride <= SIZE_MAX / sizeof(float) &&
        (size_t)n <= SIZE_MAX / (sizeof(float) * (size_t)stride)) {
      const char* source = (const char*)tensor->data + tensor->byte_offset;
      const size_t pitch = sizeof(float) * (size_t)stride;
#ifdef __HIP_PLATFORM_AMD__
      rc = hipMemcpy2D(output, width, source, pitch, width, (size_t)n, hipMemcpyDeviceToHost)
           == hipSuccess ? 0 : 1;
#else
      rc = cudaMemcpy2D(output, width, source, pitch, width, (size_t)n, cudaMemcpyDeviceToHost)
           == cudaSuccess ? 0 : 1;
#endif
    }
  }
  if (dataset.deleter) dataset.deleter(&dataset);
  return rc;
}

int seekdb_cuvs_cagra_knn(const float* base, long n, long dim,
                          const float* query, long nq, long topk,
                          unsigned int* out_ids)
{
  void* h = seekdb_cuvs_build(base, n, dim);
  if (!h) return 10;
  int rc = seekdb_cuvs_search(h, query, nq, topk, out_ids, NULL);
  seekdb_cuvs_free(h);
  return rc;
}
