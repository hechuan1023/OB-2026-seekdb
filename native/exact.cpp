// Self-contained exact flat-search kernel. No third-party vector index library.
#include <algorithm>
#include <cerrno>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <vector>

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

// Keep the reduction in its own non-inlined function: inlining it into the heap
// loop measured about 2x slower because the compiler then failed to vectorize
// the lane loop. Eight fixed lanes make the summation order independent of the
// SIMD width, so the default and AVX2 clones stay bitwise identical under
// -ffp-contract=off. Plain double accumulation is used instead of compensated
// summation: its relative error for 128 dimensions is about 1e-14, far below the
// 1e-12 relative boundary guard in vector_index.py, and it measured ~1.7x faster.
static MV_TARGET_CLONES __attribute__((noinline)) double
distance_squared(const float *__restrict v, const float *__restrict query,
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
    char buffer[512];
    const std::size_t token = static_cast<std::size_t>(p - start);
    if (token >= sizeof(buffer)) return -1;
    std::memcpy(buffer, start, token);
    buffer[token] = '\0';
    char *parsed_end = nullptr;
    // The grammar check above already restricted the token to a JSON number;
    // strtod then applies the same round-to-nearest double conversion as json.
    const double value = std::strtod(buffer, &parsed_end);
    if (parsed_end != buffer + token) return -1;
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

MV_EXPORT int mv_abi_version() { return 1; }

// Retain K+1 candidates. Python verifies a conservative boundary gap, re-evaluates
// selected distances with math.dist, and falls back on ambiguous boundaries.
MV_EXPORT MV_TARGET_CLONES std::int64_t mv_topk(
    const float *__restrict vectors, const std::int64_t *__restrict keys,
    std::uint64_t rows, std::uint64_t dimensions,
    const float *__restrict query, const std::uint64_t *__restrict selection,
    std::uint64_t selected, std::uint64_t keep,
    std::uint64_t *__restrict positions, double *__restrict distances) noexcept {
  if (!vectors || !keys || !query || !positions || !distances || !dimensions ||
      dimensions > 65536 || keep == 0 || rows > SIZE_MAX / dimensions)
    return -1;
  const std::uint64_t count = selection ? selected : rows;
  keep = std::min(keep, count);
  try {
    std::vector<Candidate> heap;
    heap.reserve(static_cast<std::size_t>(keep));
    for (std::uint64_t n = 0; n < count; ++n) {
      const std::uint64_t row = selection ? selection[n] : n;
      if (row >= rows) return -1;
      // Squared distance: monotone in the true distance, so ordering and ties
      // are unchanged. Python re-evaluates the selected rows with math.dist and
      // its boundary guard only needs a monotone key.
      const Candidate item{distance_squared(vectors + row * dimensions, query, dimensions),
                           keys[row], row};
      if (heap.size() < keep) {
        heap.push_back(item);
        std::push_heap(heap.begin(), heap.end(), better);
      } else if (better(item, heap.front())) {
        std::pop_heap(heap.begin(), heap.end(), better);
        heap.back() = item;
        std::push_heap(heap.begin(), heap.end(), better);
      }
    }
    std::sort(heap.begin(), heap.end(), better);
    for (std::size_t i = 0; i < heap.size(); ++i) {
      positions[i] = heap[i].position;
      distances[i] = heap[i].distance;
    }
    return static_cast<std::int64_t>(heap.size());
  } catch (...) {
    return -1;  // No C++ exception may escape the C ABI.
  }
}
