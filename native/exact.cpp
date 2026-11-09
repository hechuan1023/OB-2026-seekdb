// Self-contained exact flat-search kernel. No third-party vector index library.
#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <vector>

#ifdef _WIN32
#define MV_EXPORT extern "C" __declspec(dllexport)
#else
#define MV_EXPORT extern "C" __attribute__((visibility("default")))
#endif

struct Candidate {
  double distance;
  std::int64_t key;
  std::uint64_t position;
};

static bool better(const Candidate &a, const Candidate &b) {
  return a.distance < b.distance || (a.distance == b.distance && a.key < b.key);
}

MV_EXPORT int mv_abi_version() { return 1; }

// Retain K+1 candidates. Python verifies a conservative boundary gap, re-evaluates
// selected distances with math.dist, and falls back on ambiguous boundaries.
// Compensated nonnegative summation avoids dimension-dependent naive sum error.
MV_EXPORT std::int64_t mv_topk(
    const float *vectors, const std::int64_t *keys, std::uint64_t rows,
    std::uint64_t dimensions, const float *query, const std::uint64_t *selection,
    std::uint64_t selected, std::uint64_t keep, std::uint64_t *positions,
    double *distances) noexcept {
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
      const float *v = vectors + row * dimensions;
      double sum = 0.0, correction = 0.0;
      for (std::uint64_t j = 0; j < dimensions; ++j) {
        const double delta = static_cast<double>(v[j]) - query[j];
        const double term = delta * delta - correction;
        const double next = sum + term;
        correction = (next - sum) - term;
        sum = next;
      }
      const Candidate item{std::sqrt(sum), keys[row], row};
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
