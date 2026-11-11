"""Independent counterexamples for primary-key range candidate selection."""
import json
import math
import random
import struct
import tempfile
import unittest

from minivectordb.engine import Engine


def f32(value):
    return struct.unpack('<f', struct.pack('<f', value))[0]


class PKRangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Engine(self.temp.name)
        # Sparse keys on purpose: the probe path must not assume dense ids,
        # and the scan fallback must stay correct for out-of-range bounds.
        self.keys = [5, 11, 12, 13, 40, 41, 99, 100, 101, 250, 251, 999]
        self.db.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(8),v VECTOR(2))')
        for i, key in enumerate(self.keys):
            self.db.execute(f"INSERT INTO t VALUES ({key},'g{i % 2}','[{key},0]')")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def ids(self, where):
        sql = f"SELECT id FROM t {where} ORDER BY id"
        return [row[0] for row in self.db.execute(sql).rows]

    def test_range_selects(self):
        self.assertEqual(self.ids('WHERE id > 12'), [13, 40, 41, 99, 100, 101, 250, 251, 999])
        self.assertEqual(self.ids('WHERE id >= 12'), [12, 13, 40, 41, 99, 100, 101, 250, 251, 999])
        self.assertEqual(self.ids('WHERE id < 41'), [5, 11, 12, 13, 40])
        self.assertEqual(self.ids('WHERE id <= 41'), [5, 11, 12, 13, 40, 41])
        self.assertEqual(self.ids('WHERE id >= 12 AND id <= 99'), [12, 13, 40, 41, 99])
        self.assertEqual(self.ids('WHERE id > 12 AND id < 41'), [13, 40])
        self.assertEqual(self.ids('WHERE id > 999'), [])
        self.assertEqual(self.ids('WHERE id < 5'), [])
        self.assertEqual(self.ids('WHERE id >= 5 AND id < 6'), [5])
        # Contradictory range: empty, not a scan of everything.
        self.assertEqual(self.ids('WHERE id > 100 AND id < 101'), [])
        self.assertEqual(self.ids('WHERE id > 1000'), [])

    def test_range_deletes_and_updates(self):
        self.db.execute('DELETE FROM t WHERE id > 12 AND id < 99')
        self.assertEqual(self.ids(''), [5, 11, 12, 99, 100, 101, 250, 251, 999])
        self.db.execute("UPDATE t SET tag='x' WHERE id >= 99 AND id <= 101")
        # Remaining ids 5,11,12,99,100,101,250,251,999 with original tags
        # g0,g1,g0,g0,g1,g0,g1,g0,g1 — the range update rewrites 99,100,101.
        self.assertEqual([row[1] for row in self.db.execute('SELECT id,tag FROM t ORDER BY id').rows],
                         ['g0', 'g1', 'g0', 'x', 'x', 'x', 'g1', 'g0', 'g1'])
        # A delete inside a transaction must roll back to the same rows.
        self.db.execute('BEGIN')
        self.db.execute('DELETE FROM t WHERE id >= 250')
        self.assertEqual(self.ids('WHERE id >= 250'), [])
        self.db.execute('ROLLBACK')
        self.assertEqual(self.ids('WHERE id >= 250'), [250, 251, 999])

    def test_non_pk_range_still_scans(self):
        # g1 tags sit at odd insert positions: 11, 13, 41, 100, 250, 999.
        self.assertEqual([row[0] for row in self.db.execute(
            "SELECT id FROM t WHERE tag='g1' ORDER BY id").rows], [11, 13, 41, 100, 250, 999])
        # Mixed pk and non-pk conditions use the pk bound plus matches().
        self.assertEqual(self.ids("WHERE id >= 12 AND tag='g1'"), [13, 41, 100, 250, 999])

    def test_negative_and_large_bounds(self):
        self.db.execute('CREATE TABLE n(id BIGINT PRIMARY KEY,v VECTOR(1))')
        self.db.execute("INSERT INTO n VALUES (-100,'[0]'),(-5,'[0]'),(0,'[0]'),(7,'[0]')")
        rows = [row[0] for row in self.db.execute('SELECT id FROM n WHERE id >= -10 AND id <= 1 ORDER BY id').rows]
        self.assertEqual(rows, [-5, 0])
        rows = [row[0] for row in self.db.execute('SELECT id FROM n WHERE id < -50 ORDER BY id').rows]
        self.assertEqual(rows, [-100])

    def test_range_delete_matches_scan_truth(self):
        rng = random.Random(20261111)
        self.db.execute('CREATE TABLE r(id BIGINT PRIMARY KEY,v VECTOR(4))')
        keys = rng.sample(range(-5000, 5000), 300)
        for key in keys:
            vector = [f32(rng.uniform(-1, 1)) for _ in range(4)]
            self.db.execute(f"INSERT INTO r VALUES ({key},'{json.dumps(vector)}')")
        for _ in range(20):
            low = rng.randrange(-5000, 4900)
            high = rng.randrange(low, 5001)
            op = rng.choice(('>', '>='))
            op2 = rng.choice(('<', '<='))
            where = f'WHERE id {op} {low} AND id {op2} {high}'
            matching = [key for key in keys
                        if (key > low if op == '>' else key >= low) and
                           (key < high if op2 == '<' else key <= high)]
            self.db.execute(f'DELETE FROM r {where}')
            self.assertEqual(list(self.db.execute(f'SELECT id FROM r {where}').rows), [],
                             f'{where} deleted rows still visible')
            keys = [key for key in keys if key not in matching]
            self.assertEqual([row[0] for row in self.db.execute('SELECT id FROM r ORDER BY id').rows],
                             sorted(keys), where)


if __name__ == '__main__':
    unittest.main()
