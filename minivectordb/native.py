"""Optional native helpers with an exact Python fallback.

The vector parser only accepts the JSON number grammar and rejects anything
else, so callers can fall back to ``json.loads`` without changing which inputs
are valid or which error is raised.
"""
import ctypes
import os
from array import array
from pathlib import Path

_parse_vector = None
_loaded = False


def _library():
    global _parse_vector, _loaded
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
    except (OSError, AttributeError):
        _parse_vector = None
    return _parse_vector


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
