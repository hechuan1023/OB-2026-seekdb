/* Copyright (c) 2026 OceanBase.
 * Licensed under the Apache License, Version 2.0.
 */

#include <gtest/gtest.h>
#include "storage/fts/ob_fts_stop_word.h"
#include "data_plane/fts/ob_fts_parser_property.h"

using namespace oceanbase::common;

namespace oceanbase {
namespace storage {
namespace {
class FTSAggregationTest : public testing::Test
{
protected:
  void SetUp() override
  {
    meta_.set_varchar();
    meta_.set_collation_type(CS_TYPE_UTF8MB4_GENERAL_CI);
    ASSERT_EQ(OB_SUCCESS, words_.create(10, ObMemAttr("FtsTest")));
  }
  void TearDown() override { words_.destroy(); }
  int64_t count(const char *text)
  {
    ObFTWord key(STRLEN(text), text, meta_);
    int64_t result = -1;
    EXPECT_EQ(OB_SUCCESS, words_.get_refactored(key, result));
    return result;
  }
  ObArenaAllocator allocator_;
  ObFTWordMap words_;
  ObObjMeta meta_;
  ObFTParserProperty property_;
};

TEST_F(FTSAggregationTest, repeated_tokens_accumulate_without_replacing_keys)
{
  ObAddWordFlag flags;
  flags.set_groupby_word();
  ObAddWord add(property_, meta_, flags, allocator_, words_);
  for (int i = 0; i < 500; ++i) {
    ASSERT_EQ(OB_SUCCESS, add.process_word("database", 8, 8, 1));
  }
  EXPECT_EQ(500, count("database"));
  EXPECT_EQ(1, words_.size());
  EXPECT_EQ(500, add.get_add_word_count());
}

TEST_F(FTSAggregationTest, weighted_repeats_preserve_existing_initial_count)
{
  ObAddWordFlag flags;
  flags.set_groupby_word();
  ObAddWord add(property_, meta_, flags, allocator_, words_);
  ASSERT_EQ(OB_SUCCESS, add.process_word("word", 4, 4, 7));
  ASSERT_EQ(OB_SUCCESS, add.process_word("word", 4, 4, 5));
  ASSERT_EQ(OB_SUCCESS, add.process_word("word", 4, 4, 2));
  EXPECT_EQ(8, count("word"));
  EXPECT_EQ(14, add.get_add_word_count());
}

TEST_F(FTSAggregationTest, collation_equivalent_tokens_share_the_same_count)
{
  ObAddWordFlag flags;
  flags.set_groupby_word();
  ObAddWord add(property_, meta_, flags, allocator_, words_);
  ASSERT_EQ(OB_SUCCESS, add.process_word("OceanBase", 9, 9, 1));
  ASSERT_EQ(OB_SUCCESS, add.process_word("oceanbase", 9, 9, 2));
  EXPECT_EQ(1, words_.size());
  EXPECT_EQ(3, count("OCEANBASE"));
}

TEST_F(FTSAggregationTest, non_grouping_mode_keeps_duplicate_error)
{
  ObAddWordFlag flags;
  ObAddWord add(property_, meta_, flags, allocator_, words_);
  ASSERT_EQ(OB_SUCCESS, add.process_word("word", 4, 4, 1));
  EXPECT_EQ(OB_HASH_EXIST, add.process_word("word", 4, 4, 1));
  EXPECT_EQ(1, count("word"));
}

TEST_F(FTSAggregationTest, ascii_stopword_index_preserves_dictionary_collation)
{
  ObStopWordChecker checker;
  ASSERT_EQ(OB_SUCCESS, checker.init());
  const char *cases[] = {
    "a", "ABOUT", "ThE", "www", "und", "the   ", "THE ", "   ", "database",
    "verylongnonstopword", "the1", "1", "th", "they", "theory", "the-word", "the\t",
    "thé", "ＴＨＥ", "数据库", "一", "😀", "tHe!", "a b", "café"
  };
  ObObjMeta dictionary_meta;
  dictionary_meta.set_varchar();
  dictionary_meta.set_collation_type(CS_TYPE_UTF8MB4_GENERAL_CI);
  for (const ObCollationType collation : {CS_TYPE_UTF8MB4_BIN, CS_TYPE_UTF8MB4_GENERAL_CI}) {
    ObObjMeta input_meta;
    input_meta.set_varchar();
    input_meta.set_collation_type(collation);
    for (const char *text : cases) {
      bool expected = false;
      ObFTWord reference(STRLEN(text), text, dictionary_meta);
      for (const auto &stopword : ob_stop_word_list) {
        expected |= reference == ObFTWord(STRLEN(stopword), stopword, dictionary_meta);
      }
      bool actual = !expected;
      ASSERT_EQ(OB_SUCCESS, checker.check_stopword(ObFTWord(STRLEN(text), text, input_meta), actual));
      EXPECT_EQ(expected, actual) << text << " collation=" << collation;
    }
  }
}

TEST_F(FTSAggregationTest, binary_and_unsupported_collations_retain_existing_behavior)
{
  ObStopWordChecker checker;
  ASSERT_EQ(OB_SUCCESS, checker.init());
  for (const ObCollationType collation : {CS_TYPE_BINARY}) {
    for (const char *text : {"THE", "about", "database", "ThE "}) {
      ObString converted;
      ASSERT_EQ(OB_SUCCESS, ObCharset::charset_convert(allocator_, ObString(text),
          CS_TYPE_UTF8MB4_BIN, collation, converted));
      ObObjMeta input_meta;
      input_meta.set_varchar();
      input_meta.set_collation_type(collation);
      bool actual = false;
      ASSERT_EQ(OB_SUCCESS, checker.check_stopword(
          ObFTWord(converted.length(), converted.ptr(), input_meta), actual));
      EXPECT_EQ(0 != STRCMP(text, "database"), actual);
    }
  }
  // These enum values exist, but seekdb's supported collation set excludes
  // them. Keep the original conversion error instead of treating their bytes
  // as UTF-8 or ASCII.
  for (const ObCollationType collation : {CS_TYPE_UTF16_BIN, CS_TYPE_UTF16LE_BIN, CS_TYPE_LATIN1_SWEDISH_CI}) {
    ObObjMeta input_meta;
    input_meta.set_varchar();
    input_meta.set_collation_type(collation);
    bool actual = false;
    EXPECT_EQ(OB_INVALID_ARGUMENT, checker.check_stopword(ObFTWord(3, "the", input_meta), actual));
  }
}
} // namespace
} // namespace storage
} // namespace oceanbase
