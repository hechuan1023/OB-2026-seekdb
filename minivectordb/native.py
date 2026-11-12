"""Optional native helpers with an exact Python fallback.

The vector parser only accepts the JSON number grammar and rejects anything
else, so callers can fall back to ``json.loads`` without changing which inputs
are valid or which error is raised.
"""
import ctypes
import math
import os
from array import array
from pathlib import Path

_parse_vector = None
_parse_vectors = None
_exact_distance = None
_exact_distances = None
_loaded = False


def _library():
    global _parse_vector, _parse_vectors, _exact_distance, _exact_distances, _loaded
    if _loaded:
        return _parse_vector
    _loaded = True
    filename = '_native.dll' if os.name == 'nt' else '_native.so'
    try:
        library = ctypes.CDLL(str(Path(__file__).with_name(filename)))
        if library.mv_abi_version() != 1:
            return None
        parse = library.mv_parse_vector
        parse.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
        parse.restype = ctypes.c_int64
        _parse_vector = parse
        try:
            batch_parse = library.mv_parse_vectors
        except AttributeError:
            _parse_vectors = None
        else:
            batch_parse.argtypes = [ctypes.POINTER(ctypes.c_char_p),
                                    ctypes.POINTER(ctypes.c_size_t),
                                    ctypes.c_size_t, ctypes.c_size_t, ctypes.c_void_p]
            batch_parse.restype = ctypes.c_int64
            _parse_vectors = batch_parse
        exact = library.mv_exact_distance
        exact.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        exact.restype = ctypes.c_double
        _exact_distance = exact
        batch = library.mv_exact_distances
        batch.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_size_t, ctypes.c_size_t, ctypes.c_void_p]
        batch.restype = ctypes.c_int64
        _exact_distances = batch
    except (OSError, AttributeError):
        _parse_vector = _parse_vectors = _exact_distance = _exact_distances = None
    return _parse_vector


def distance(vector, query):
    """Exact double distance for two float32 buffers.

    Uses the same ordered double loop as the native batch path, so the indexed
    and generic executors return identical bits. Falls back to math.dist when
    the native library is unavailable (both executors then use math.dist).
    """
    if _exact_distance is None:
        return math.dist(vector, query)
    return _exact_distance(vector.buffer_info()[0], query.buffer_info()[0], len(vector))


def distances(vectors_address, query, positions):
    """Exact distances for snapshot positions, or None without the native library."""
    if _exact_distances is None:
        return None
    count = len(positions)
    out = array('d', [0.0]) * count
    returned = _exact_distances(vectors_address, query.buffer_info()[0],
                                positions.buffer_info()[0], count, len(query),
                                out.buffer_info()[0])
    if returned != count:
        return None
    return out


def parse_vector(text, size):
    """Return array('f') for a JSON number array, or None to use json.loads."""
    parse = _library()
    if parse is None:
        return None
    try:
        raw = text.encode('utf-8')
    except UnicodeEncodeError:
        return None
    values = array('f', [0.0]) * size
    if parse(raw, len(raw), values.buffer_info()[0], size) != size:
        return None
    return values


def parse_vectors(texts, size):
    """Parse multiple JSON vectors, or return None to use the per-value path."""
    parse = _library()
    if parse is None or _parse_vectors is None or not texts:
        return None
    try:
        encoded = [text.encode('utf-8') for text in texts]
    except UnicodeEncodeError:
        return None
    pointers = (ctypes.c_char_p * len(encoded))(*encoded)
    lengths = (ctypes.c_size_t * len(encoded))(*(len(raw) for raw in encoded))
    flat = array('f', [0.0]) * (size * len(encoded))
    returned = _parse_vectors(pointers, lengths, len(encoded), size,
                              flat.buffer_info()[0])
    if returned != len(encoded):
        return None
    return [array('f', flat[start:start + size])
            for start in range(0, len(flat), size)]
