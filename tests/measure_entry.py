"""Measurement-only wrapper: original CLI plus snapshot build events on stderr."""
import json
import sys
import time

sys.path.insert(0, '/app')
from minivectordb import vector_index
from minivectordb.__main__ import main

original = vector_index.Snapshot.__init__


def measured_init(self, *args, **kwargs):
    start = time.perf_counter()
    original(self, *args, **kwargs)
    print('MVDB_BUILD ' + json.dumps({
        'rows': self.count,
        'logical_bytes': sum(path.stat().st_size for path in self.paths),
        'seconds': time.perf_counter() - start,
    }), file=sys.stderr, flush=True)


vector_index.Snapshot.__init__ = measured_init
raise SystemExit(main())
