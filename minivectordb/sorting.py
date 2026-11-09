"""Bounded-memory ORDER BY with TopK pruning and disk merge runs."""
import heapq
import os
import pickle
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass

from .storage import read_record, write_record


@dataclass(frozen=True)
class Descending:
    value: object

    def __lt__(self, other):
        return self.value > other.value


def file_rows(file):
    size = os.fstat(file.fileno()).st_size
    while file.tell() < size:
        yield read_record(file, file.tell())


class Sorter:
    def __init__(self, directory, memory_bytes, limit):
        self.directory = directory
        self.budget = memory_bytes
        self.limit = limit
        self.buffer = []
        self.bytes = 0
        self.levels = []
        self.paths = set()

    def add(self, key, row):
        entry = (key, row)
        if self.limit is not None:
            if self.limit == 0:
                return
            if len(self.buffer) >= self.limit:
                if not key < self.buffer[0][0].value:
                    return
                _, _, previous_size = heapq.heappop(self.buffer)
                self.bytes -= previous_size
            size = len(pickle.dumps(entry, protocol=5)) * 4 + 256
            heapq.heappush(self.buffer, (Descending(key), entry, size))
        else:
            size = len(pickle.dumps(entry, protocol=5)) * 4 + 256
            self.buffer.append(entry)
        self.bytes += size
        if self.bytes >= self.budget:
            self._flush()

    def _sorted(self):
        items = [item[1] for item in self.buffer] if self.limit is not None else self.buffer
        return sorted(items, key=lambda item: item[0])

    def _write_run(self, entries):
        fd, path = tempfile.mkstemp(prefix='sort-', suffix='.tmp', dir=self.directory)
        self.paths.add(path)
        with os.fdopen(fd, 'w+b') as file:
            for n, entry in enumerate(entries):
                if self.limit is not None and n >= self.limit:
                    break
                write_record(file, entry)
        return path

    def _merge(self, paths):
        with ExitStack() as stack:
            files = [stack.enter_context(open(path, 'rb')) for path in paths]
            yield from heapq.merge(*(file_rows(file) for file in files), key=lambda item: item[0])

    def _flush(self):
        if not self.buffer:
            return
        path = self._write_run(self._sorted())
        self.buffer = []
        self.bytes = 0
        level = 0
        while True:
            if len(self.levels) <= level:
                self.levels.append([])
            self.levels[level].append(path)
            if len(self.levels[level]) < 16:
                break
            old_paths = self.levels[level]
            merged = self._merge(old_paths)
            try:
                path = self._write_run(merged)
            finally:
                merged.close()
            for old in old_paths:
                os.unlink(old)
                self.paths.remove(old)
            self.levels[level] = []
            level += 1

    def results(self):
        if not self.paths:
            yield from (entry[1] for entry in self._sorted())
            return
        self._flush()
        paths = [path for level in self.levels for path in level]
        # Limit simultaneously open files even when several merge levels remain.
        while len(paths) > 16:
            group = paths[:16]
            merged = self._merge(group)
            try:
                new = self._write_run(merged)
            finally:
                merged.close()
            for path in group:
                os.unlink(path)
                self.paths.remove(path)
            paths = paths[16:] + [new]
        merged = self._merge(paths)
        try:
            for i, (_, row) in enumerate(merged):
                if self.limit is not None and i >= self.limit:
                    break
                yield row
        finally:
            merged.close()

    def close(self):
        for path in self.paths:
            os.unlink(path)
        self.paths.clear()
