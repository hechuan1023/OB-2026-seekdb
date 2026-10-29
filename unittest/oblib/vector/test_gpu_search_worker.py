#!/usr/bin/env python3
"""Compile the production synchronous worker without the GPU dependency tree."""
import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile
import os


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cxx',default=shutil.which('c++') or shutil.which('g++'))
    args=parser.parse_args()
    if not args.cxx:
        parser.error('a C++17 compiler is required (--cxx)')
    root=Path(__file__).resolve().parents[3]
    source=(root/'src/oblib/lib/vector/ob_vsag_adaptor.cpp').read_text()
    start=source.index('// Synchronous reusable large-stack worker.')
    end=source.index('struct ObCuvsJob {',start)
    unit='''
#include <pthread.h>
#include <mutex>
#include <condition_variable>
#include <exception>
#include <thread>
#include <vector>
#include <atomic>
#include <cassert>
#include <stdexcept>
'''+source[start:end]+'''
struct Job { int input; int result; };
static void* calculate(void* arg) {
  auto* j=static_cast<Job*>(arg); j->result=j->input*7+3; return nullptr;
}
static void* fail(void*) { throw std::runtime_error("expected"); }
int main() {
  std::atomic<int> checked{0};
  {
    CuvsSearchWorker worker;
    std::vector<std::thread> callers;
    for (int i=0;i<8;++i) callers.emplace_back([&worker,&checked,i] {
      for(int n=0;n<1000;++n) {
        Job j{i*1000+n,0};
        assert(worker.run(calculate,&j));
        assert(j.result==j.input*7+3); ++checked;
      }
    });
    for(auto& caller:callers) caller.join();
    bool caught=false;
    try { worker.run(fail,nullptr); } catch(const std::runtime_error&) { caught=true; }
    assert(caught);
    Job j{99,0}; assert(worker.run(calculate,&j)); assert(j.result==696);
  }
  assert(checked==8000);
  for(int i=0;i<8;++i) {
    CuvsSearchWorker worker;
    Job j{i,0}; assert(worker.run(calculate,&j)); assert(j.result==i*7+3);
  }
  Job j{5,0}; assert(ob_cuvs_search_worker().run(calculate,&j)); assert(j.result==38);
}
'''
    with tempfile.TemporaryDirectory(prefix='seekdb-gpu-worker-') as directory:
        path=Path(directory)
        cpp=path/'test.cpp'
        binary=path/('test.exe' if os.name=='nt' else 'test')
        cpp.write_text(unit)
        subprocess.run([args.cxx,'-std=c++17','-O2','-Wall','-Wextra','-Werror',
                        '-pthread',str(cpp),'-o',str(binary)],check=True)
        subprocess.run([str(binary)],check=True,timeout=60)
    print('PASS: 8000 concurrent jobs, exception recovery and worker teardown')


if __name__=='__main__':
    main()
