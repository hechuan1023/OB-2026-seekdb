/* Copyright (c) 2026 OceanBase.
 * Licensed under the Apache License, Version 2.0.
 */
#include <gtest/gtest.h>
#include <algorithm>
#include <vector>
#include "storage/blocksstable/encoding/ob_integer_base_diff_decoder.h"

using namespace oceanbase::common;
using namespace oceanbase::blocksstable;

TEST(IntegerBatchWidth, packed_values_match_scalar_with_nulls_and_permutations)
{
  for (const ObObjType type : {ObIntType, ObUInt64Type, ObFloatType, ObDoubleType}) {
    for (int width = 1; width <= 64; ++width) {
      for (bool nullable : {false, true}) {
        SCOPED_TRACE(::testing::Message() << type << ':' << width << ':' << nullable);
        constexpr int rows = 67;
        ObMicroBlockHeader micro;
        micro.row_count_ = rows;
        micro.extend_value_bit_ = nullable ? 1 : 0;
        ObColumnHeader column;
        column.type_ = ObColumnHeader::INTEGER_BASE_DIFF;
        column.obj_type_ = ObUInt64Type;
        column.length_ = sizeof(ObIntegerBaseDiffHeader) + sizeof(uint64_t);
        column.attr_ = ObColumnHeader::FIX_LENGTH | ObColumnHeader::BIT_PACKING
            | (nullable ? ObColumnHeader::HAS_EXTEND_VALUE : 0);
        const int prefix = nullable ? rows : 0;
        const int byte_count = (prefix + rows * width + 7) / 8;
        std::vector<char> bytes(column.length_ + byte_count, 0);
        auto *header = new (bytes.data()) ObIntegerBaseDiffHeader;
        header->length_ = width;
        uint64_t base = UINT64_MAX - 23;
        MEMCPY(bytes.data() + sizeof(*header), &base, sizeof(base));
        auto *data = reinterpret_cast<unsigned char *>(bytes.data() + column.length_);
        auto set_bit = [&](int bit) { data[bit / 8] |= 1U << (bit % 8); };
        std::vector<uint64_t> expected(rows);
        const uint64_t mask = width == 64 ? UINT64_MAX : (uint64_t(1) << width) - 1;
        for (int row = 0; row < rows; ++row) {
          uint64_t delta = (uint64_t(row + 1) * 0x9e3779b97f4a7c15ULL) & mask;
          expected[row] = base + delta;
          if (nullable && row % 5 == 1) { set_bit(row); }
          for (int bit = 0; bit < width; ++bit) {
            if ((delta >> bit) & 1) { set_bit(prefix + row * width + bit); }
          }
        }
        ObColumnDecoderCtx ctx;
        ctx.micro_block_header_ = &micro;
        ctx.col_header_ = &column;
        ctx.obj_meta_.set_type(type);
        ctx.cache_attributes_[ObColumnHeader::FIX_LENGTH] = true;
        ctx.cache_attributes_[ObColumnHeader::HAS_EXTEND_VALUE] = nullable;
        ctx.cache_attributes_[ObColumnHeader::BIT_PACKING] = true;
        ObIntegerBaseDiffDecoder decoder;
        ASSERT_EQ(OB_SUCCESS, decoder.init(micro, column, bytes.data()));
        std::vector<int32_t> ids;
        for (int i = 0; i < rows; ++i) { ids.push_back((i * 19) % rows); }
        ids.push_back(rows - 1);
        ids.push_back(0);
        std::vector<ObDatum> output(ids.size());
        std::vector<uint64_t> storage(ids.size());
        for (size_t i = 0; i < ids.size(); ++i) {
          output[i].ptr_ = reinterpret_cast<const char *>(&storage[i]);
        }
        ASSERT_EQ(OB_SUCCESS, decoder.batch_decode(ctx, nullptr, ids.data(), nullptr,
                                                  ids.size(), output.data()));
        uint32_t len = 0;
        ASSERT_EQ(OB_SUCCESS, get_uint_data_datum_len(ObDatum::get_obj_datum_map_type(type), len));
        for (size_t i = 0; i < ids.size(); ++i) {
          bool is_null = nullable && ids[i] % 5 == 1;
          ASSERT_EQ(is_null, output[i].is_null());
          if (!is_null) {
            ASSERT_EQ(len, output[i].len_);
            EXPECT_EQ(0, MEMCMP(output[i].ptr_, &expected[ids[i]], len));
            ObDatum scalar;
            uint64_t scalar_storage = 0;
            scalar.ptr_ = reinterpret_cast<const char *>(&scalar_storage);
            ObBitStream bits(data, byte_count);
            ASSERT_EQ(OB_SUCCESS, decoder.decode(ctx, scalar, ids[i], bits,
                                                 bytes.data(), bytes.size()));
            EXPECT_EQ(0, MEMCMP(output[i].ptr_, scalar.ptr_, len));
          }
        }
      }
    }
  }
}
