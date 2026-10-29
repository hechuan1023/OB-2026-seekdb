#!/usr/bin/env python3
"""AMD CAGRA build/search benchmark for the hipVS contest task.

Runs against an ALREADY STARTED seekdb observer and measures exactly what the
official judge measures for the build score:

  * the offline DDL path   CREATE VECTOR INDEX ... WITH (lib=cuvs, ...)
  * the first fixed query  (part of the build window)
  * nq single-row APPROXIMATE queries (QPS / P95 / Recall@10 / Recall@100)

The CAGRA build parameters are read by libseekdb_cuvs_bridge.so from the
environment of the observer process, so run one observer per profile.

Usage (inside the AMD Radeon Cloud "seekdb-hipvs" template):

    python3 amd_cagra_bench.py --rows 250000 --dim 128 --nq 4096 \
        --label fast --evidence /tmp/amd-ab

The script only talks to the local observer through the `mysql` CLI and writes
a JSON record per run into <evidence>/<label>.json.
"""
import argparse
import json
import math
import os
import random
import subprocess
import sys
import time

MYSQL_BIN = os.environ.get("MYSQL_BIN", "mysql")


def mysql_cmd(port, user):
    return [MYSQL_BIN, "-h127.0.0.1", "-P%d" % port, "-u%s" % user, "--local-infile=1"]


def sql(port, user, query, timed=False):
    t0 = time.time()
    r = subprocess.run(mysql_cmd(port, user) + ["-e", query],
                       capture_output=True, text=True)
    dt = time.time() - t0
    if timed:
        return dt, r.returncode, r.stdout, r.stderr
    return r.returncode, r.stdout, r.stderr


def sql_rows(port, user, query):
    r = subprocess.run(mysql_cmd(port, user) + ["-N", "-e", query],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("query failed: %s\n%s" % (query, r.stderr))
    return [ln.split("\t") for ln in r.stdout.strip().splitlines() if ln]


def vec_literal(values):
    return "[" + ",".join("%.6f" % v for v in values) + "]"


def generate_rows(rows, dim, rng):
    """Deterministic float32 base vectors; numpy when available."""
    try:
        import numpy as np
        base = np.random.default_rng(20261029).standard_normal((rows, dim)).astype("float32")
        queries = np.random.default_rng(20261030).standard_normal((256, dim)).astype("float32")
        return base, queries
    except ImportError:
        base = [[rng.gauss(0.0, 1.0) for _ in range(dim)] for _ in range(rows)]
        queries = [[rng.gauss(0.0, 1.0) for _ in range(dim)] for _ in range(256)]
        return base, queries


def load_table(port, user, db, table, dim, base, data_dir):
    """Create an empty vector table and load the base vectors into it."""
    sql(port, user, "drop database if exists %s; create database %s;" % (db, db))
    sql(port, user, "use %s; create table %s(id bigint primary key, v vector(%d));" % (db, table, dim))

    csv_path = os.path.join(data_dir, "%s_base.csv" % db)
    with open(csv_path, "w", encoding="utf-8") as f:
        for i, row in enumerate(base):
            f.write("%d\t%s\n" % (i, vec_literal(row)))

    rc, out, err = sql(port, user,
                       "use %s; set ob_query_timeout=600000000; set ob_trx_timeout=600000000; load data local infile '%s' into table %s "
                       "fields terminated by '\\t' (id, v);" % (db, csv_path.replace("\\", "/"), table))
    if rc != 0:
        print("LOAD DATA LOCAL failed, fallback to INSERT: %s" % err.strip()[:200])
        # Fall back to batched INSERT when LOAD DATA LOCAL INFILE is unavailable.
        sql(port, user, "use %s; set ob_sql_work_area_percentage=100; delete from %s;" % (db, table))
        step = 500
        for start in range(0, len(base), step):
            chunk = base[start:start + step]
            values = ",".join("(%d,%s)" % (start + j, vec_literal(r))
                              for j, r in enumerate(chunk))
            stmt = "use %s; set ob_query_timeout=600000000; insert into %s values %s;" % (db, table, values)
            r = subprocess.run(mysql_cmd(port, user), input=stmt, capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError("insert failed at row %d: %s" % (start, r.stderr))
    os.remove(csv_path)


def create_index(port, user, db, table, k):
    """Offline DDL path, exactly like the judge."""
    ddl = ("use %s; create vector index idx_items on %s(v) "
           "with (distance=l2, type=hnsw, lib=cuvs, m=16, ef_construction=200, ef_search=64);"
           % (db, table))
    dt, rc, out, err = sql(port, user, ddl, timed=True)
    if rc != 0:
        # Older syntax: inline index on the table.
        sql(port, user, "use %s; drop table if exists %s;" % (db, table))
        raise RuntimeError("create vector index failed: %s" % err)
    return dt


def first_query(port, user, db, table, q, k):
    dt, rc, out, err = sql(port, user,
                           "use %s; select id from %s order by l2_distance(v, %s) approximate limit %d;"
                           % (db, table, vec_literal(q), k), timed=True)
    if rc != 0:
        raise RuntimeError("first query failed: %s" % err)
    return dt, out


def run_queries(port, user, db, table, queries, k):
    """nq single-row APPROXIMATE queries: QPS, P95 latency, collected ids."""
    lat = []
    got = []
    for q in queries:
        dt, rc, out, err = sql(port, user,
                               "use %s; select id from %s order by l2_distance(v, %s) approximate limit %d;"
                               % (db, table, vec_literal(q), k), timed=True)
        if rc != 0:
            raise RuntimeError("query failed: %s" % err)
        lat.append(dt)
        ids = [int(ln.split("\t")[0]) for ln in out.strip().splitlines() if ln]
        got.append(ids)
    lat.sort()
    p95 = lat[min(len(lat) - 1, int(math.ceil(0.95 * len(lat))) - 1)]
    return lat, p95, got


def exact_knn(base, queries, k):
    """Brute-force ground truth (numpy)."""
    try:
        import numpy as np
        b = np.asarray(base, dtype="float32")
        q = np.asarray(queries, dtype="float32")
        d = ((q[:, None, :] - b[None, :, :]) ** 2).sum(axis=2)
        idx = np.argpartition(d, k, axis=1)[:, :k]
        rows = np.arange(len(q))[:, None]
        order = np.argsort(d[rows, idx], axis=1)
        return idx[rows, order].tolist()
    except ImportError:
        return None


def recall(got, truth, k):
    hit = 0
    tot = 0
    for g, t in zip(got, truth):
        ts = set(t[:k])
        for x in g[:k]:
            tot += 1
            if x in ts:
                hit += 1
    return round(hit / tot, 4) if tot else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=2881)
    ap.add_argument("--user", default="root")
    ap.add_argument("--rows", type=int, default=250000)
    ap.add_argument("--dim", type=int, default=128)
    ap.add_argument("--nq", type=int, default=4096)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--recall-samples", type=int, default=128)
    ap.add_argument("--db", default="amdbench")
    ap.add_argument("--table", default="items")
    ap.add_argument("--label", default="run")
    ap.add_argument("--evidence", default="/tmp/amd-ab")
    ap.add_argument("--data-dir", default="/tmp/amd-ab-data")
    args = ap.parse_args()

    os.makedirs(args.evidence, exist_ok=True)
    os.makedirs(args.data_dir, exist_ok=True)
    rng = random.Random(20261029)

    rc, out, err = sql(args.port, args.user, "select version();")
    if rc != 0:
        print("cannot reach the observer on port %d: %s" % (args.port, err), file=sys.stderr)
        return 2
    version = out.strip().splitlines()[0] if out.strip() else "?"

    print("observer: %s" % version)
    print("generating %d x %d vectors ..." % (args.rows, args.dim))
    base, qpool = generate_rows(args.rows, args.dim, rng)

    load_table(args.port, args.user, args.db, args.table, args.dim, base, args.data_dir)
    print("loading done, creating the GPU index (this is the scored build window) ...")
    build_s = create_index(args.port, args.user, args.db, args.table, args.k)
    print("build window (CREATE VECTOR INDEX): %.3f s" % build_s)

    first_s, _ = first_query(args.port, args.user, args.db, args.table, qpool[0], args.k)
    print("first query: %.3f s" % first_s)

    sample = qpool[:args.recall_samples]
    nq = min(args.nq, len(qpool) * 16)
    queries = [qpool[i % len(qpool)] for i in range(nq)]
    print("running %d single-row queries ..." % nq)
    lat, p95, got = run_queries(args.port, args.user, args.db, args.table, queries, args.k)

    truth = exact_knn(base, sample, args.k)
    rec10 = recall(got[:len(sample)], truth, args.k) if truth else None
    rec100 = None
    if truth is not None:
        _, _, got100 = run_queries(args.port, args.user, args.db, args.table, sample, 100)
        truth100 = exact_knn(base, sample, 100) or truth
        rec100 = recall(got100, truth100, 100)

    total = sum(lat)
    record = {
        "label": args.label,
        "rows": args.rows,
        "dim": args.dim,
        "nq": nq,
        "k": args.k,
        "build_seconds": round(build_s, 3),
        "first_query_seconds": round(first_s, 3),
        "query_qps": round(nq / total, 2) if total else 0.0,
        "query_p95_ms": round(p95 * 1000, 3),
        "query_avg_ms": round(total / max(1, nq) * 1000, 3),
        "recall_at_10": rec10,
        "recall_at_100": rec100,
        "env": {k: v for k, v in os.environ.items() if k.startswith("OB_CUVS_")},
        "observer_version": version,
    }
    out_path = os.path.join(args.evidence, "%s.json" % args.label)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)
    print(json.dumps(record, indent=2, ensure_ascii=False))
    print("written: %s" % out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
