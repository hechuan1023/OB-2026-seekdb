/* Copyright (c) 2026 OceanBase.
 * Licensed under the Apache License, Version 2.0.
 */
#include <gtest/gtest.h>
#include <vector>
#include "storage/retrieval/ob_text_daat_iter.h"
#include "sql/engine/ob_exec_context.h"

using namespace oceanbase::common;
using namespace oceanbase::sql;
using namespace oceanbase::storage;

namespace {
class PostingIterator : public ObISRDaaTDimIter
{
public:
  std::vector<int64_t> ids;
  int64_t cursor = -1;
  int64_t value = 0;
  int fail_at = -2;
  ObDatum datum;
  int get_next_row() override
  {
    if (++cursor == fail_at) return OB_ERR_UNEXPECTED;
    if (cursor >= static_cast<int64_t>(ids.size())) return OB_ITER_END;
    value = ids[cursor];
    datum.ptr_ = reinterpret_cast<char *>(&value);
    datum.set_int(value);
    return OB_SUCCESS;
  }
  int get_curr_id(const ObDatum *&id) const override { id = &datum; return OB_SUCCESS; }
  int get_curr_score(double &score) const override { score = 1.0; return OB_SUCCESS; }
};

class SmallTextMergeTest : public testing::Test
{
protected:
  ObArenaAllocator allocator;
  ObExecContext context{allocator};
  ObEvalCtx eval{context};
  alignas(ObDatum) char storage[512]{};
  char *frames[1]{storage};
  ObExpr id;
  ObLimitParam limit;
  ObSparseRetrievalMergeParam param;
  ObTextDaaTParam text_param;
  ObSRDaaTInnerProductRelevanceCollector collector;
  PostingIterator postings[5];
  ObSEArray<ObISRDaaTDimIter *, 5> dims;
  ObTextDaaTIter iterator;

  void SetUp() override
  {
    eval.frames_ = frames;
    eval.set_max_batch_size(4);
    eval.reuse(4);
    id.frame_idx_ = 0;
    id.datum_off_ = 0;
    id.eval_info_off_ = 128;
    id.eval_flags_off_ = 160;
    id.res_buf_off_ = 256;
    id.res_buf_len_ = 8;
    id.batch_result_ = true;
    id.datum_meta_.type_ = ObUInt64Type;
    for (int i = 0; i < 4; ++i) new (reinterpret_cast<ObDatum *>(storage) + i) ObDatum();
    new (storage + id.eval_info_off_) ObEvalInfo();
    id.locate_datums_for_update(eval, 4);
    param.id_proj_expr_ = &id;
    param.eval_ctx_ = &eval;
    param.limit_param_ = &limit;
    param.max_batch_size_ = 4;
    text_param.base_param_ = &param;
    text_param.dim_iters_ = &dims;
    text_param.allocator_ = &allocator;
    text_param.relevance_collector_ = &collector;
  }
  void TearDown() override
  {
    // Production owns ObTextRetrievalDaaTTokenIter instances. These test doubles
    // need no token-specific cleanup in ObTextDaaTIter::reset().
    dims.reuse();
  }
  void init(int count, int minimum = 1)
  {
    for (int i = 0; i < count; ++i) ASSERT_EQ(OB_SUCCESS, dims.push_back(&postings[i]));
    ASSERT_EQ(OB_SUCCESS, collector.init(minimum));
    ASSERT_EQ(OB_SUCCESS, iterator.init(text_param));
  }
  std::vector<int64_t> read(int capacity)
  {
    std::vector<int64_t> result;
    int ret = OB_SUCCESS;
    while (ret == OB_SUCCESS) {
      int64_t count = 0;
      ret = iterator.get_next_rows(capacity, count);
      EXPECT_TRUE(ret == OB_SUCCESS || ret == OB_ITER_END);
      if (ret != OB_SUCCESS && ret != OB_ITER_END) break;
      for (int i = 0; i < count; ++i) result.push_back(id.locate_batch_datums(eval)[i].get_int());
    }
    return result;
  }
};

TEST_F(SmallTextMergeTest, equal_heads_empty_lists_and_batch_boundaries)
{
  postings[0].ids = {1, 3, 5, 7, 9, 11};
  postings[1].ids = {1, 2, 3};
  postings[2].ids = {};
  postings[3].ids = {3, 4, 5, 6, 7, 8, 10};
  init(4);
  const std::vector<int64_t> expected{1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11};
  for (int capacity : {1, 3, 4}) {
    EXPECT_EQ(expected, read(capacity));
    // Exercise the base reset of merge state without token-specific test casts.
    iterator.ObSRDaaTIterImpl::reuse();
    for (auto &posting : postings) posting.cursor = -1;
  }
}

TEST_F(SmallTextMergeTest, collector_keeps_minimum_match_requirement)
{
  postings[0].ids = {1, 3, 5, 7};
  postings[1].ids = {1, 2, 3};
  postings[2].ids = {3, 5, 6};
  init(3, 2);
  EXPECT_EQ((std::vector<int64_t>{1, 3, 5}), read(2));
}

TEST_F(SmallTextMergeTest, single_list_and_empty_result)
{
  init(1);
  EXPECT_TRUE(read(4).empty());
  postings[0].ids = {2, 4, 6};
  postings[0].cursor = -1;
  iterator.ObSRDaaTIterImpl::reuse();
  EXPECT_EQ((std::vector<int64_t>{2, 4, 6}), read(2));
}

TEST_F(SmallTextMergeTest, five_lists_keep_heap_path)
{
  for (int i = 0; i < 5; ++i) postings[i].ids = {1, i + 2, 9};
  init(5);
  EXPECT_EQ((std::vector<int64_t>{1, 2, 3, 4, 5, 6, 9}), read(3));
}

TEST_F(SmallTextMergeTest, posting_error_is_propagated)
{
  postings[0].ids = {1, 2};
  postings[0].fail_at = 1;
  init(1);
  int64_t count = 0;
  EXPECT_EQ(OB_ERR_UNEXPECTED, iterator.get_next_rows(4, count));
}
} // namespace
