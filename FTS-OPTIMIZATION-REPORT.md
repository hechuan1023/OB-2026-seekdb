# 全文索引优化与验证记录

测试日期：2026-10-26。上游基线为官方 2026-competition 分支的
5bce9e06d1281347ad61344e4b01fab9158c58a2。候选版本为本报告所在提交。
保留原有 IK 优化，并增加词频与查询聚合优化；没有修改基准数据、SQL、计时、
评分脚本或固定基线。

## 瓶颈依据与修改

1. **IK 逐字符处理**：复用已有的单次 Unicode 解码、中文数字集合二分查找、
   UTF-8 二进制边界比较及按 offset 前进的仲裁链游标。补齐无匹配链时的扫描位置推进，
   使推进逻辑不依赖 keep_single 分支。
2. **词频聚合**：ObAddWord::groupby_word 原先先 get_refactored，再
   set_refactored(overwrite)。每次出现重复词均重复计算带字符集语义的哈希、比较和加锁，
   且覆盖操作会分配新节点、释放旧节点。改用现有 set_or_update，单次定位并原地累加。
   保留首个词频为 1、后续累加 word_freq 的既有行为，以及不分组模式的重复键错误。
   没有更改字符集、大小写折叠或停用词规则。
3. **查询聚合**：TAAT 分区按照约千条文档的目标规模划分，但每个哈希表原先只请求
   10 个桶。ObHashMap 的默认 EXTEND_RATIO=1，不自动扩容。改按文档数与分区数估算，
   每个分区保留 10 至 OB_HASHMAP_DEFAULT_SIZE 个桶；预留规模有上限。
   相关度通过同一个 set_or_update 原地累加，保留原来的浮点加法顺序和桶锁。
   不更改扫描数据、分区方式、相关度公式、谓词或 LIMIT 语义。

## 构建与测试

在 WSL2 的既有 Release 工具链和同基线编译产物上，重新编译相关 SQL、FTS 和 IK
Unity 单元并链接独立二进制。编译过程中保留项目的 -Werror 设置。
验证实例使用独立运行目录和 SQL 端口，不操作其他运行中的实例。

- 新增 4 项 C++ 聚合测试全部通过：大量重复词、带权频次、字符集等价词、不分组重复错误。
- 基线与候选的 414 组 TOKENIZE 输出完全相同，覆盖 IK smart、IK max_word、BENG、
  中英文混合、繁简中文数字、全角字符、日文、韩文、重音字符、emoji 和扩展区汉字。
- 300 条记录上的 40 条查询对照全部一致：逐 ID 和相关度核对，包含插入、更新、删除、
  追加、过滤、排序及 LIMIT；没有仅用命中数量代替结果比较。
- 官方 fts_index 套件 15 项中 13 项通过：basic_dml_sequel、cn_word、
  create_fts_index_afterward、create_table_with_fts_index、domain_id_merge_with_compare_null、
  drop_index、index_view、load_data、offline_ddl_with_fts_index、
  online_ddl_on_table_with_fts_index、parser_properties、simple_query、tokenize_function。
- 另外两项在**未优化的原始基线也有相同失败**：
  partitioned_simple_query 的 EXPLAIN 预期参数编号 :0 与实际 :1 不同；
  basic_dml 第 1598 行因系统表查询未取得 rowkey_doc_table_id，展开为 No such row 后报错。
  不将这两项记为通过，也没有修改预期结果来掩盖差异。

Mysqltest 在临时目录将 Windows 检出的 test/result 文件恢复为 LF 换行，并在隔离实例
配置套件要求的本地 admin 用户；官方文件内容未修改。

语义对照采集器：

    python3 unittest/storage/fts/fts_sql_snapshot.py \
      --client=/path/to/obclient --port=2893 > snapshot.json

在两种二进制上分别采集，再逐项比较 JSON。该脚本只创建、使用和删除自己的
fts_semantics_regression 数据库。

## 完整性能对照

环境：Intel Core i5-12450H，WSL2 Ubuntu x86_64，10 个逻辑 CPU，
约 11 GiB 内存，Clang 17，Release -O2。同一机器与编译方式，
按 baseline/candidate 交替执行三组；测量期间没有运行源码编译。

每轮都运行原版 fts_large_bench.sh，参数固定：

    ROWS=20000 BATCH=500 ROUNDS=3000 QUERY_ROUNDS=200 \
    SAMPLES=3 WARMUP=30 SKIP_LOAD=0

每轮使用新报告文件，完整重建数据库、重新导入及构建全部三个索引。
以下为三轮指标的中位数；正数为耗时下降，负数为退化。

| 指标 | 原始基线 | 候选 | 耗时下降 |
| --- | ---: | ---: | ---: |
| build_ik_all_sec | 8.858 | 8.721 | 1.55% |
| build_ik_content_sec | 6.363 | 6.269 | 1.48% |
| build_beng_en_sec | 3.659 | 3.451 | 5.68% |
| tokenize_ik_avg_ms | 0.1920 | 0.1856 | 3.33% |
| tokenize_beng_avg_ms | 0.1456 | 0.1280 | 12.09% |
| query_cn_avg_ms | 1.7733 | 1.6785 | 5.35% |
| query_beng_avg_ms | 2.7312 | 2.4898 | 8.84% |
| query_mixed_avg_ms | 2.1179 | 2.0271 | 4.29% |
| query_limit_avg_ms | 2.5637 | 2.6259 | -2.43% |

构建类平均下降 2.90%，分词类 7.71%，查询类 4.01%，三类等权为 **4.87%**。
计算包含 LIMIT 的负提升，没有逐项截断，也没有把九项直接平均。
三轮存在波动，这些观测不构成统计显著性结论。

所有六轮报告的 cn/beng/mixed/limit 命中数量分别保持 8001/11000/7332/20。
固定官方评分脚本接受全部候选报告。由于本机未优化版本也远快于固定 CI 基线，
本机套用固定基线的分数不能代表代码带来的加速，更不能保证正式满分。
正式成绩须由统一评测环境测量。

本机原始报告、逐轮指标、语义快照及测试日志保存在工作区旁：
_build/fts-validation-20261026。该目录是本地验证产物，不参与产品执行。
