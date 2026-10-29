#!/usr/bin/env python3
"""Compile production snapshot framing with an in-memory bridge double (Linux)."""
import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cxx', default=shutil.which('c++') or shutil.which('g++'))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    source = (root / 'src/oblib/lib/vector/ob_vsag_adaptor.cpp').read_text()
    start = source.index('static const char OB_CUVS_SNAPSHOT_MAGIC')
    end = source.index('}  // anonymous namespace', start)
    unit = r'''
#include <algorithm>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <pthread.h>
#include <set>
#include <sstream>
#include <unistd.h>
#include <vector>
#define LOG_INFO(...) ((void)0)
#define LOG_WARN(...) ((void)0)
enum { OB_SUCCESS=0, OB_ERR_UNEXPECTED=-1, OB_ERR_VSAG_RETURN_ERROR=-2, OB_CUVS_MIN_PTS=2 };
struct ObTimeUtility { static int64_t current_time() { return 0; } };
struct MockIndex { long n, dim; std::vector<float> values; };
static int handles=0;
static bool fail_copy=false;
static void seekdb_cuvs_free(void* h) { if(h) { delete static_cast<MockIndex*>(h); --handles; } }
static std::shared_ptr<void> ob_cuvs_wrap(void* h) { return {h,seekdb_cuvs_free}; }
static std::shared_ptr<void> ob_cuvs_build_big_stack(const float* v,long n,long d) {
  ++handles; return ob_cuvs_wrap(new MockIndex{n,d,std::vector<float>(v,v+n*d)});
}
static int seekdb_cuvs_serialize(void* h,const char* path) {
  auto* i=static_cast<MockIndex*>(h); std::ofstream f(path,std::ios::binary);
  f.write(reinterpret_cast<char*>(&i->n),sizeof(i->n));
  f.write(reinterpret_cast<char*>(&i->dim),sizeof(i->dim));
  f.write(reinterpret_cast<char*>(i->values.data()),i->values.size()*sizeof(float));
  return f.good()?0:-1;
}
static void* seekdb_cuvs_deserialize(const char* path,long n,long d) {
  std::ifstream f(path,std::ios::binary); long an=0,ad=0;
  f.read(reinterpret_cast<char*>(&an),sizeof(an)); f.read(reinterpret_cast<char*>(&ad),sizeof(ad));
  if(an!=n || ad!=d) return nullptr;
  auto* i=new MockIndex{n,d,std::vector<float>(n*d)};
  f.read(reinterpret_cast<char*>(i->values.data()),n*d*sizeof(float));
  if(!f.good()) { delete i; return nullptr; } ++handles; return i;
}
static int seekdb_cuvs_copy_dataset(void* h,float* out,long n,long d) {
  if(fail_copy) return -1;
  auto* i=static_cast<MockIndex*>(h); assert(i->n==n && i->dim==d);
  memcpy(out,i->values.data(),n*d*sizeof(float)); return 0;
}
struct ObCuvsEntry {
  std::shared_ptr<void> bridge_; int dim_=0;
  std::vector<int64_t> ids_,buf_ids_; std::vector<float> buf_vecs_; size_t built_n_=0;
};
static std::mutex g_ob_cuvs_mu,g_ob_cuvs_marked_mu;
static std::map<void*,ObCuvsEntry*> g_ob_cuvs_reg;
static std::set<void*> g_ob_cuvs_marked;
''' + source[start:end] + r'''
static void clear() {
  for(auto& entry:g_ob_cuvs_reg) delete entry.second;
  g_ob_cuvs_reg.clear(); g_ob_cuvs_marked.clear(); assert(handles==0);
}
static std::string legacy(const std::string& compact,const std::vector<float>& values) {
  std::string old=compact; old[7]='2';
  old.insert(32+4*sizeof(int64_t),reinterpret_cast<const char*>(values.data()),values.size()*sizeof(float));
  return old;
}
static void read(const std::string& bytes,bool compact,int expected,const std::vector<float>& values) {
  std::istringstream in(bytes.substr(8));
  int result=ob_cuvs_deserialize(reinterpret_cast<void*>(2),3,in,compact);
  assert((result==0)==(expected==0));
  if(result==0) {
    auto* entry=g_ob_cuvs_reg.at(reinterpret_cast<void*>(2));
    assert(entry->buf_ids_==std::vector<int64_t>({19,22,100,1000}));
    assert(entry->buf_vecs_==values && entry->ids_==entry->buf_ids_ && entry->built_n_==4);
    assert(g_ob_cuvs_marked.count(reinterpret_cast<void*>(2))==1);
  } else { assert(g_ob_cuvs_reg.empty()); assert(handles==0); }
  clear();
}
int main() {
  const std::vector<float> values{0,-0.f,1,2,3,4,5,6,7,8,9,10};
  auto* e=new ObCuvsEntry; e->dim_=3; e->buf_ids_={19,22,100,1000}; e->buf_vecs_=values;
  g_ob_cuvs_reg[reinterpret_cast<void*>(1)]=e;
  std::ostringstream stream; int ret=0;
  assert(ob_cuvs_serialize(reinterpret_cast<void*>(1),stream,ret) && ret==0);
  std::string compact=stream.str(); assert(compact.substr(0,8)=="OBCUVS03");
  // Header + IDs + bridge blob; the FP32 dataset occurs exactly once.
  assert(compact.size()==32+4*sizeof(int64_t)+2*sizeof(long)+values.size()*sizeof(float));
  clear(); read(compact,true,0,values);
  const std::string old=legacy(compact,values); read(old,false,0,values);
  for(bool v3:{false,true}) {
    const std::string& full=v3?compact:old;
    for(size_t len=8;len<full.size();++len) read(full.substr(0,len),v3,-1,values);
    std::string corrupt=full; corrupt[20]=1; read(corrupt,v3,-1,values); // reserved
    corrupt=full; corrupt[16]=4; read(corrupt,v3,-1,values); // wrong dimensions
    corrupt=full; memset(&corrupt[24],255,8); read(corrupt,v3,-1,values); // invalid count
  }
  fail_copy=true; read(compact,true,-1,values); fail_copy=false;
  read(compact,true,0,values);
}
'''
    with tempfile.TemporaryDirectory(prefix='seekdb-snapshot-') as directory:
        cpp = Path(directory) / 'test.cpp'
        binary = Path(directory) / 'test'
        cpp.write_text(unit)
        subprocess.run([args.cxx, '-std=c++17', '-O1', '-pthread', str(cpp), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True, timeout=60)
    print('PASS: v2/v3 framing, payload size, ID/vector restoration, truncations and copy failure cleanup')


if __name__ == '__main__':
    main()
