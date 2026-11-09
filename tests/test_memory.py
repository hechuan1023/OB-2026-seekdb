"""A local allocation guard; tracemalloc is explicitly NOT cgroup memory.peak."""
import tempfile
import tracemalloc
import unittest
from pathlib import Path

from minivectordb.engine import Engine


class MemoryTests(unittest.TestCase):
    def test_disk_data_exceeds_python_allocation_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Engine(directory, sort_memory=256 * 1024, cache_memory=256 * 1024)
            db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,payload VARCHAR(8192))')
            payload = 'x' * 8192
            for start in range(0, 4096, 32):
                db.execute('INSERT INTO t VALUES ' + ','.join(f"({i},'{payload}')" for i in range(start, start + 32)))
            db.close()
            self.assertGreater((Path(directory) / 'database.wal').stat().st_size, 32 * 1024 * 1024)
            tracemalloc.start()
            db = Engine(directory, sort_memory=256 * 1024, cache_memory=256 * 1024)
            try:
                count = 0
                for row in db.execute('SELECT * FROM t ORDER BY id DESC').rows:
                    self.assertEqual(row[0], 4095 - count)
                    self.assertEqual(row[1], payload)
                    count += 1
                self.assertEqual(count, 4096)
                _, peak = tracemalloc.get_traced_memory()
                self.assertLess(peak, 8 * 1024 * 1024)
                self.assertEqual(list(Path(directory).glob('sort-*.tmp')), [])
            finally:
                db.close()
                tracemalloc.stop()


if __name__ == '__main__':
    unittest.main()
