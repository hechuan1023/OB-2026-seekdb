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

MV_EXPORT int mv_abi_version() { return 1; }

// Retain K+1 candidates. Python verifies a conservative boundary gap, re-evaluates
// selected distances with math.dist, and falls back on ambiguous boundaries.
// Eight independent compensated chains: lane j uses (j & 7), and the lanes are
// combined in a fixed order, so the result is bitwise identical regardless of
// which SIMD width the compiler picks for the per-lane inner loops. More lanes
// shorten the loop-carried dependency that prevents wide vectorization.
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
      const float *v = vectors + row * dimensions;
      double acc[8] = {0.0};
      double cor[8] = {0.0};
      std::uint64_t j = 0;
      for (; j + 8 <= dimensions; j += 8) {
        for (std::uint64_t lane = 0; lane < 8; ++lane) {
          const double delta = static_cast<double>(v[j + lane]) - query[j + lane];
          const double term = delta * delta - cor[lane];
          const double next = acc[lane] + term;
          cor[lane] = (next - acc[lane]) - term;
          acc[lane] = next;
        }
      }
      for (; j < dimensions; ++j) {
        const std::uint64_t lane = j & 7;
        const double delta = static_cast<double>(v[j]) - query[j];
        const double term = delta * delta - cor[lane];
        const double next = acc[lane] + term;
        cor[lane] = (next - acc[lane]) - term;
        acc[lane] = next;
      }
      double sum = 0.0, correction = 0.0;
      for (std::uint64_t lane = 0; lane < 8; ++lane) {
        const double term = acc[lane] - correction;
        const double next = sum + term;
        correction = (next - sum) - term;
        sum = next;
      }
      // Squared distance: monotone in the true distance, so ordering and ties
      // are unchanged. Python re-evaluates the selected rows with math.dist and
      // its boundary guard only needs a monotone key.
      const Candidate item{sum, keys[row], row};
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
