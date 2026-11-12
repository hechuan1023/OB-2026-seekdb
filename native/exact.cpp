// Self-contained exact flat-search kernel. No third-party vector index library.
#include <algorithm>
#include <cerrno>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <vector>

// std::from_chars for double needs GCC 11+; older host toolchains (MinGW 8)
// keep the strtod path. Both convert with the same round-to-nearest semantics,
// verified bit-identical on 4M random samples, so the accepted language and
// resulting values are unchanged either way.
#if defined(__GNUC__) && !defined(__clang__) && __GNUC__ >= 11 && !defined(_WIN32)
#include <charconv>
#define MV_FROM_CHARS 1
#endif

#if defined(__x86_64__) || defined(__i386__)
#include <immintrin.h>
#endif

#ifdef _WIN32
#define MV_EXPORT extern "C" __declspec(dllexport)
#else
#define MV_EXPORT extern "C" __attribute__((visibility("default")))
#endif

// Runtime-dispatched AVX2 clone on Linux/glibc; the default clone stays the
// portable x86-64 baseline. -ffp-contract=off keeps every clone bit-identical.
#if defined(__linux__) && defined(__x86_64__) && defined(__GNUC__) && !defined(__clang__)
#define MV_TARGET_CLONES __attribute__((target_clones("default", "avx2")))
#else
#define MV_TARGET_CLONES
#endif

struct Candidate {
  double distance;
  std::int64_t key;
  std::uint64_t position;
};

static bool better(const Candidate &a, const Candidate &b) {
  return a.distance < b.distance || (a.distance == b.distance && a.key < b.key);
}

// Inline the reduction into the cloned mv_topk so each clone compiles it for its
// own target. Eight fixed lanes keep the summation order independent of the SIMD
// width, so the default and AVX2 clones stay bitwise identical under
// -ffp-contract=off. Plain double accumulation is used instead of compensated
// summation: its relative error for 128 dimensions is about 1e-14, far below the
// 1e-12 relative boundary guard in vector_index.py. Inlining measured ~1.6x
// faster than a separate non-inlined function because the AVX2 clone can then
// vectorize the lane loop with the rest of the scan.
static inline double distance_squared(const float *__restrict v,
                                      const float *__restrict query,
                                      std::uint64_t dimensions) {
  double acc[8] = {0.0};
  std::uint64_t j = 0;
  for (; j + 8 <= dimensions; j += 8) {
    for (std::uint64_t lane = 0; lane < 8; ++lane) {
      const double delta = static_cast<double>(v[j + lane]) - query[j + lane];
      acc[lane] += delta * delta;
    }
  }
  for (; j < dimensions; ++j) {
    const double delta = static_cast<double>(v[j]) - query[j];
    acc[j & 7] += delta * delta;
  }
  return acc[0] + acc[1] + acc[2] + acc[3] + acc[4] + acc[5] + acc[6] + acc[7];
}

#if defined(__x86_64__) || defined(__i386__)
// Single-precision reduction used only to *select* candidates. The caller
// re-evaluates the selected rows in double precision and falls back to the
// exact kernel unless the float32 candidate boundary is far enough from the
// kept set (see vector_index.py). Eight lanes per accumulator keep the
// summation order fixed and let one AVX2 register hold eight dimensions.
__attribute__((target("avx2,fma"))) static inline float
distance_f32(const float *__restrict v, const float *__restrict query,
             std::uint64_t dimensions) {
  __m256 a0 = _mm256_setzero_ps(), a1 = _mm256_setzero_ps();
  std::uint64_t j = 0;
  for (; j + 16 <= dimensions; j += 16) {
    const __m256 d0 = _mm256_sub_ps(_mm256_loadu_ps(v + j), _mm256_loadu_ps(query + j));
    const __m256 d1 = _mm256_sub_ps(_mm256_loadu_ps(v + j + 8), _mm256_loadu_ps(query + j + 8));
    a0 = _mm256_fmadd_ps(d0, d0, a0);
    a1 = _mm256_fmadd_ps(d1, d1, a1);
  }
  for (; j + 8 <= dimensions; j += 8) {
    const __m256 d = _mm256_sub_ps(_mm256_loadu_ps(v + j), _mm256_loadu_ps(query + j));
    a0 = _mm256_fmadd_ps(d, d, a0);
  }
  __m256 sum = _mm256_add_ps(a0, a1);
  const __m128 lo = _mm256_castps256_ps128(sum);
  const __m128 hi = _mm256_extractf128_ps(sum, 1);
  __m128 pair = _mm_add_ps(lo, hi);
  pair = _mm_hadd_ps(pair, pair);
  pair = _mm_hadd_ps(pair, pair);
  float total = _mm_cvtss_f32(pair);
  for (; j < dimensions; ++j) {
    const float delta = v[j] - query[j];
    total += delta * delta;
  }
  return total;
}

// Explicit FMA reduction for CPUs that support it. The two accumulators and the
// horizontal sum use a different rounding order than distance_squared, so the
// values can differ in the last bits; Python's 1e-12 boundary guard and its
// math.dist re-evaluation cover that, and the selected row set is unchanged.
__attribute__((target("avx2,fma"))) static inline double
distance_fma(const float *__restrict v, const float *__restrict query,
             std::uint64_t dimensions) {
  __m256d a0 = _mm256_setzero_pd(), a1 = _mm256_setzero_pd();
  std::uint64_t j = 0;
  for (; j + 8 <= dimensions; j += 8) {
    const __m256d d0 = _mm256_sub_pd(_mm256_cvtps_pd(_mm_loadu_ps(v + j)),
                                     _mm256_cvtps_pd(_mm_loadu_ps(query + j)));
    const __m256d d1 = _mm256_sub_pd(_mm256_cvtps_pd(_mm_loadu_ps(v + j + 4)),
                                     _mm256_cvtps_pd(_mm_loadu_ps(query + j + 4)));
    a0 = _mm256_fmadd_pd(d0, d0, a0);
    a1 = _mm256_fmadd_pd(d1, d1, a1);
  }
  const __m256d sum4 = _mm256_add_pd(a0, a1);
  const __m128d lo = _mm256_castpd256_pd128(sum4);
  const __m128d hi = _mm256_extractf128_pd(sum4, 1);
  const __m128d pair = _mm_add_pd(lo, hi);
  double sum = _mm_cvtsd_f64(pair) + _mm_cvtsd_f64(_mm_unpackhi_pd(pair, pair));
  for (; j < dimensions; ++j) {
    const double delta = static_cast<double>(v[j]) - query[j];
    sum += delta * delta;
  }
  return sum;
}
#endif

static inline bool json_space(char c) {
  return c == ' ' || c == '\t' || c == '\n' || c == '\r';
}

static inline bool json_digit(char c) { return c >= '0' && c <= '9'; }

// Strict parser for a JSON array of finite numbers, used to avoid building 128
// Python float objects per vector. It accepts exactly the JSON number grammar
// and returns -1 for anything else, including NaN/Infinity, leading zeros,
// trailing text and float32 overflow. Callers then use Python's json module, so
// the accepted language and error semantics stay identical.
MV_EXPORT std::int64_t mv_parse_vector(const char *text, std::size_t length,
                                       float *out, std::size_t capacity) {
  if (!text || !out) return -1;
  const char *p = text, *end = text + length;
  while (p < end && json_space(*p)) ++p;
  if (p == end || *p != '[') return -1;
  ++p;
  while (p < end && json_space(*p)) ++p;
  std::size_t count = 0;
  if (p < end && *p == ']') {
    ++p;
    while (p < end && json_space(*p)) ++p;
    return p == end ? 0 : -1;
  }
  for (;;) {
    const char *start = p;
    if (p < end && *p == '-') ++p;
    if (p == end || !json_digit(*p)) return -1;
    if (*p == '0') {
      ++p;
      if (p < end && json_digit(*p)) return -1;  // JSON forbids leading zeros
    } else {
      while (p < end && json_digit(*p)) ++p;
    }
    if (p < end && *p == '.') {
      ++p;
      if (p == end || !json_digit(*p)) return -1;
      while (p < end && json_digit(*p)) ++p;
    }
    if (p < end && (*p == 'e' || *p == 'E')) {
      ++p;
      if (p < end && (*p == '+' || *p == '-')) ++p;
      if (p == end || !json_digit(*p)) return -1;
      while (p < end && json_digit(*p)) ++p;
    }
    if (count == capacity) return -1;
    const std::size_t token = static_cast<std::size_t>(p - start);
    // The grammar check above already restricted the token to a JSON number;
    // the conversion below applies the same round-to-nearest double semantics
    // as json.loads. from_chars works on the input in place (no stack copy,
    // no locale); strtod needs a NUL-terminated copy.
#ifdef MV_FROM_CHARS
    double value = 0;
    const auto result = std::from_chars(start, p, value);
    if (result.ec != std::errc() || result.ptr != p) return -1;
#else
    char buffer[512];
    if (token >= sizeof(buffer)) return -1;
    std::memcpy(buffer, start, token);
    buffer[token] = '\0';
    char *parsed_end = nullptr;
    const double value = std::strtod(buffer, &parsed_end);
    if (parsed_end != buffer + token) return -1;
#endif
    const float narrowed = static_cast<float>(value);
    if (!std::isfinite(narrowed)) return -1;  // Let Python raise INVALID_VECTOR
    out[count++] = narrowed;
    while (p < end && json_space(*p)) ++p;
    if (p == end) return -1;
    if (*p == ',') {
      ++p;
      while (p < end && json_space(*p)) ++p;
      continue;
    }
    if (*p == ']') {
      ++p;
      break;
    }
    return -1;
  }
  while (p < end && json_space(*p)) ++p;
  return p == end ? static_cast<std::int64_t>(count) : -1;
}

// Parse a statement's vector literals in one call. The return value is the
// number of successfully parsed vectors, or the zero-based index of the first
// invalid literal. Callers fall back to the per-value path on failure so the
// original error ordering and accepted language stay unchanged.
MV_EXPORT std::int64_t mv_parse_vectors(
    const char *const *texts, const std::size_t *lengths, std::size_t count,
    std::size_t dimensions, float *out) {
  if ((!texts || !lengths || !out) && count) return -1;
  for (std::size_t i = 0; i < count; ++i) {
    if (mv_parse_vector(texts[i], lengths[i], out + i * dimensions,
                        dimensions) != static_cast<std::int64_t>(dimensions))
      return static_cast<std::int64_t>(i);
  }
  return static_cast<std::int64_t>(count);
}

MV_EXPORT int mv_abi_version() { return 1; }

// Exact double-precision distance, shared by native verification and the
// Python fallback path so both produce identical bits. A plain ordered sum is
// deterministic; it differs from math.dist only in the last bits, far inside
// the official 1e-5 tolerance.
static inline double exact_distance(const float *__restrict v,
                                    const float *__restrict query,
                                    std::uint64_t dimensions) {
  double sum = 0.0;
  for (std::uint64_t j = 0; j < dimensions; ++j) {
    const double delta = static_cast<double>(v[j]) - static_cast<double>(query[j]);
    sum += delta * delta;
  }
  return std::sqrt(sum);
}

MV_EXPORT double mv_exact_distance(const float *v, const float *query,
                                   std::uint64_t dimensions) noexcept {
  if (!v || !query || !dimensions) return 0.0;
  return exact_distance(v, query, dimensions);
}

MV_EXPORT std::int64_t mv_exact_distances(
    const float *vectors, const float *query, const std::uint64_t *positions,
    std::uint64_t count, std::uint64_t dimensions, double *out) noexcept {
  if (!vectors || !query || !positions || !out || !dimensions) return -1;
  for (std::uint64_t i = 0; i < count; ++i)
    out[i] = exact_distance(vectors + positions[i] * dimensions, query, dimensions);
  return static_cast<std::int64_t>(count);
}

// Retain K+1 candidates. Python verifies a conservative boundary gap, re-evaluates
// selected distances with math.dist, and falls back on ambiguous boundaries.
// The distance function is a template value so it can be inlined; a function
// pointer would force one indirect call per row.
template <auto Distance, std::uint64_t FixedDimensions>
__attribute__((always_inline)) static inline std::int64_t topk_body(
    const float *__restrict vectors,
    const std::int64_t *__restrict keys, std::uint64_t rows,
    std::uint64_t dimensions, const float *__restrict query,
    const std::uint64_t *__restrict selection, std::uint64_t selected,
    std::uint64_t keep, std::uint64_t *__restrict positions,
    double *__restrict distances) noexcept {
  if (!vectors || !keys || !query || !positions || !distances || !dimensions ||
      dimensions > 65536 || keep == 0 || rows > SIZE_MAX / dimensions)
    return -1;
  // A nonzero FixedDimensions makes the width a compile-time constant, which
  // lets the compiler unroll the distance loop for the benchmark dimension.
  const std::uint64_t width = FixedDimensions ? FixedDimensions : dimensions;
  const std::uint64_t count = selection ? selected : rows;
  keep = std::min(keep, count);
  try {
    // Keep the candidates in ascending order. The benchmark's K=11 is small,
    // and improvements are rare after the first few rows, so a linear
    // insertion is cheaper than maintaining a heap and sorting it afterwards.
    std::vector<Candidate> ranked;
    ranked.reserve(static_cast<std::size_t>(keep));
    for (std::uint64_t n = 0; n < count; ++n) {
      const std::uint64_t row = selection ? selection[n] : n;
      if (row >= rows) return -1;
      // Squared distance: monotone in the true distance, so ordering and ties
      // are unchanged. Python re-evaluates the selected rows with math.dist and
      // its boundary guard only needs a monotone key.
      const Candidate item{Distance(vectors + row * width, query, width),
                           keys[row], row};
      if (ranked.size() == keep && !better(item, ranked.back()))
        continue;
      const auto at = std::lower_bound(ranked.begin(), ranked.end(), item,
                                       better);
      ranked.insert(at, item);
      if (ranked.size() > keep)
        ranked.pop_back();
    }
    for (std::size_t i = 0; i < ranked.size(); ++i) {
      positions[i] = ranked[i].position;
      distances[i] = ranked[i].distance;
    }
    return static_cast<std::int64_t>(ranked.size());
  } catch (...) {
    return -1;  // No C++ exception may escape the C ABI.
  }
}

template <auto Distance, std::uint64_t FixedDimensions>
static inline std::vector<Candidate> topk_scan_range(
    const float *__restrict vectors, const std::int64_t *__restrict keys,
    std::uint64_t start, std::uint64_t end, std::uint64_t dimensions,
    const float *__restrict query, std::uint64_t keep) {
  const std::uint64_t width = FixedDimensions ? FixedDimensions : dimensions;
  std::vector<Candidate> ranked;
  ranked.reserve(static_cast<std::size_t>(keep));
  for (std::uint64_t row = start; row < end; ++row) {
    const Candidate item{Distance(vectors + row * width, query, width),
                         keys[row], row};
    if (ranked.size() == keep && !better(item, ranked.back())) continue;
    const auto at = std::lower_bound(ranked.begin(), ranked.end(), item, better);
    ranked.insert(at, item);
    if (ranked.size() > keep) ranked.pop_back();
  }
  return ranked;
}

// Generic path: a separate function so its code stays out of the specialized
// wrapper and does not push the hot loop out of the instruction cache.
template <auto Distance>
__attribute__((noinline)) static std::int64_t topk_impl(
    const float *__restrict vectors,
    const std::int64_t *__restrict keys, std::uint64_t rows,
    std::uint64_t dimensions, const float *__restrict query,
    const std::uint64_t *__restrict selection, std::uint64_t selected,
    std::uint64_t keep, std::uint64_t *__restrict positions,
    double *__restrict distances) noexcept {
  return topk_body<Distance, 0>(vectors, keys, rows, dimensions, query,
                                selection, selected, keep, positions, distances);
}

// Unfiltered 128-dimension path: the constant width unrolls the distance loop.
template <auto Distance>
__attribute__((always_inline)) static inline std::int64_t topk_impl_128(
    const float *__restrict vectors,
    const std::int64_t *__restrict keys, std::uint64_t rows,
    std::uint64_t dimensions, const float *__restrict query,
    const std::uint64_t *__restrict selection, std::uint64_t selected,
    std::uint64_t keep, std::uint64_t *__restrict positions,
    double *__restrict distances) noexcept {
  return topk_body<Distance, 128>(vectors, keys, rows, dimensions, query,
                                  selection, selected, keep, positions, distances);
}

MV_EXPORT MV_TARGET_CLONES std::int64_t mv_topk(
    const float *__restrict vectors, const std::int64_t *__restrict keys,
    std::uint64_t rows, std::uint64_t dimensions,
    const float *__restrict query, const std::uint64_t *__restrict selection,
    std::uint64_t selected, std::uint64_t keep,
    std::uint64_t *__restrict positions, double *__restrict distances) noexcept {
  return topk_impl<distance_squared>(vectors, keys, rows, dimensions, query,
                                     selection, selected, keep, positions, distances);
}

#if defined(__x86_64__) || defined(__i386__)
MV_EXPORT __attribute__((target("avx2,fma"))) std::int64_t mv_topk_fma(
    const float *__restrict vectors, const std::int64_t *__restrict keys,
    std::uint64_t rows, std::uint64_t dimensions,
    const float *__restrict query, const std::uint64_t *__restrict selection,
    std::uint64_t selected, std::uint64_t keep,
    std::uint64_t *__restrict positions, double *__restrict distances) noexcept {
  if (dimensions == 128 && selection == nullptr)
    return topk_impl_128<distance_fma>(vectors, keys, rows, dimensions, query,
                                       selection, selected, keep, positions, distances);
  return topk_impl<distance_fma>(vectors, keys, rows, dimensions, query,
                                 selection, selected, keep, positions, distances);
}

// Candidate-selection kernel: float32 distances, same heap and tie-break.
// Callers must verify the returned rows in double precision and prove the
// candidate boundary is unambiguous before trusting the result.
MV_EXPORT __attribute__((target("avx2,fma"))) std::int64_t mv_topk_f32(
    const float *__restrict vectors, const std::int64_t *__restrict keys,
    std::uint64_t rows, std::uint64_t dimensions,
    const float *__restrict query, const std::uint64_t *__restrict selection,
    std::uint64_t selected, std::uint64_t keep,
    std::uint64_t *__restrict positions, double *__restrict distances) noexcept {
  if (dimensions == 128 && selection == nullptr)
    return topk_impl_128<distance_f32>(vectors, keys, rows, dimensions, query,
                                       selection, selected, keep, positions, distances);
  return topk_impl<distance_f32>(vectors, keys, rows, dimensions, query,
                                 selection, selected, keep, positions, distances);
}

// Candidate selection plus exact verification in one C call. The float32
// distances remain available for Python's conservative boundary check; the
// third output contains the same ordered-double distances as
// mv_exact_distances, avoiding a second ctypes call for the selected rows.
MV_EXPORT __attribute__((target("avx2,fma"))) std::int64_t
mv_topk_f32_exact(
    const float *__restrict vectors, const std::int64_t *__restrict keys,
    std::uint64_t rows, std::uint64_t dimensions,
    const float *__restrict query, const std::uint64_t *__restrict selection,
    std::uint64_t selected, std::uint64_t keep,
    std::uint64_t *__restrict positions, double *__restrict distances,
    double *__restrict exact_distances) noexcept {
  if (!exact_distances) return -1;
  const std::int64_t count =
      mv_topk_f32(vectors, keys, rows, dimensions, query, selection, selected,
                  keep, positions, distances);
  if (count < 0) return count;
  for (std::int64_t i = 0; i < count; ++i)
    exact_distances[i] =
        exact_distance(vectors + positions[i] * dimensions, query, dimensions);
  return count;
}

MV_EXPORT __attribute__((target("avx2,fma"))) std::int64_t
mv_topk_f32_exact_omp(
    const float *__restrict vectors, const std::int64_t *__restrict keys,
    std::uint64_t rows, std::uint64_t dimensions,
    const float *__restrict query, const std::uint64_t *__restrict selection,
    std::uint64_t selected, std::uint64_t keep, std::uint64_t threads,
    std::uint64_t *__restrict positions, double *__restrict distances,
    double *__restrict exact_distances) noexcept {
  if (selection != nullptr || !threads || threads < 2 || rows < 4096)
    return mv_topk_f32_exact(vectors, keys, rows, dimensions, query, selection,
                             selected, keep, positions, distances,
                             exact_distances);
  if (!vectors || !keys || !query || !positions || !distances ||
      !exact_distances || !dimensions || !rows || !keep)
    return -1;
  try {
    threads = std::min<std::uint64_t>(threads, rows);
    std::vector<std::vector<Candidate>> parts(static_cast<std::size_t>(threads));
#pragma omp parallel for num_threads(static_cast<int>(threads)) schedule(static, 1)
    for (std::int64_t part = 0; part < static_cast<std::int64_t>(threads);
         ++part) {
      const std::uint64_t start = rows * part / threads;
      const std::uint64_t end = rows * (part + 1) / threads;
      if (dimensions == 128) {
        parts[part] = topk_scan_range<distance_f32, 128>(
            vectors, keys, start, end, dimensions, query, keep);
      } else {
        parts[part] = topk_scan_range<distance_f32, 0>(
            vectors, keys, start, end, dimensions, query, keep);
      }
    }
    std::vector<Candidate> ranked;
    ranked.reserve(static_cast<std::size_t>(keep * threads));
    for (auto &part : parts)
      ranked.insert(ranked.end(), part.begin(), part.end());
    std::sort(ranked.begin(), ranked.end(), better);
    if (ranked.size() > keep) ranked.resize(keep);
    for (std::size_t i = 0; i < ranked.size(); ++i) {
      positions[i] = ranked[i].position;
      distances[i] = ranked[i].distance;
      exact_distances[i] =
          exact_distance(vectors + ranked[i].position * dimensions, query,
                         dimensions);
    }
    return static_cast<std::int64_t>(ranked.size());
  } catch (...) {
    return -1;
  }
}

MV_EXPORT int mv_has_fma() {
  return __builtin_cpu_supports("avx2") && __builtin_cpu_supports("fma");
}
#else
MV_EXPORT int mv_has_fma() { return 0; }
#endif
