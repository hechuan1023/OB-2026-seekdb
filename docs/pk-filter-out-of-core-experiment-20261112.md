# 主键范围向量过滤：超内存 I/O 优化实验（2026-11-12）

## 假设与实现

在 c423f50619c6140bcd9411db91428ad7a5d87710 基线上，64 MiB、18000×1024 压力测试中，多个仅包含主键范围的向量查询通过 `VectorIndexes.selection()` 对快照每个主键调用 `storage.get`，反复读取包含完整 1024 维向量的记录。快照本身已经保存主键。候选实现仅在所有过滤条件均为主键条件时，对快照主键做相同比较，跳过删除 tombstone，并保留 filter_budget 超限回退；其他列或混合条件走原路径。行可见性、删除和回滚依旧沿用现有失效机制。不改变普通写入、事务日志或提交持久化。

## 超内存测试

脚本 `.local-pressure-stages.py`，在独立 Docker 容器中执行原本 `tests.container_vector_pressure` 的完整断言，附加各阶段 cgroup 快照。2 CPU、64 MiB、禁 swap、18000×1024、向量文件 73,728,000 B。四个主键范围查询含 0、1%、80%、100% 匹配率；时间含测试真值和探针。基线镜像 `minivectordb:diag-test-c423` 三轮范围阶段：13.087、13.064、11.950 s；设备 8:64 读取增量 1,050,554,368、1,044,574,208、1,042,776,064 B；累计 peak 均 64 MiB。原始 `.local-pressure-stages-20261125.log`、`.local-pressure-baseline2.log`、`.local-pressure-baseline3.log`。

候选第一版镜像 `minivectordb:pk-filter-v1` 三轮范围阶段：0.726、0.646、0.664 s；读取增量 144,056,320、143,122,432、143,241,216 B；peak 均 64 MiB。后续只调整非主键条件短路判断后的最终候选 `minivectordb:pk-filter-candidate` 又测一轮：0.696 s，143,613,952 B，峰值 64 MiB（以原始日志为准）；全部原有压力断言通过，包括事务、失败语句、重启、SIGKILL。日志 `.local-pressure-candidate1.log`、`.local-pressure-candidate2.log`、`.local-pressure-candidate3.log`、`.local-pressure-candidate-v2.log`。最终候选轮数仅一轮，不能称其自身三轮中位数。

与基线中位数约 13.064 s、1,044.57 MB 比较，最终候选单轮约 0.696 s、143.61 MB；工作负载范围阶段耗时约下降 95%，本机观测设备读取约下降 86%。第一版三轮方向一致，支持同一核心改动的可重复收益。cgroup 包含测试进程和子进程，设备归属、缓存、readahead 与写回未按官方环境验证；结果不是官方性能 E，不表示正式 10000×128 benchmark 也会提升。

## 常规负载与其他指标

两个独立随机种子，运行 `.local-pkfilter-paired.py`，每 seed 两镜像独立写入/查询容器各四轮，轮换顺序；10000×128、每批 100 自动提交、1000 次 exact 查询、一半 tag 等值过滤、预热 4 条，所有查询逐条核验，写入完成后独立重启核验 10000 行。两个最终候选结果 `.local-pkfilter-v2-paired-20261127.json` 与 `.local-pkfilter-v2-paired-20261128.json`，基线同 c423。

| seed | 版本 | 写入 T 中位数 s | 查询 T 中位数 s | 写/查累计内存峰值 MiB | 本机 8:64 写/查 I/O 增量 B |
|---|---|---:|---:|---:|---:|
| 20261127 | 基线 | 1.8114 | 1.7714 | 23.72 / 34.49 | 6,086,656 / 6,037,504 |
| 20261127 | 候选 | 1.8661 | 1.7108 | 23.33 / 34.55 | 6,086,656 / 6,037,504 |
| 20261128 | 基线 | 1.8305 | 1.8815 | 23.33 / 34.49 | 6,086,656 / 6,037,504 |
| 20261128 | 候选 | 1.7350 | 1.8273 | 23.32 / 34.45 | 6,086,656 / 6,037,504 |

候选第一版曾在 seed 20261127 测得查询中位数 1.824 s、基线 1.732 s，故对非主键条件增加短路后重新构建并复测；不得隐去旧数据，原始 `.local-pkfilter-paired-20261126.json` 与 `.local-pkfilter-paired-20261127.json`。最终版 seed 20261127 写入中位数较慢约 3%，seed 20261128 则较快约 5%，写入代码未改但轮次有波动；尚不能严格证明没有小幅回退，也不能声称稳定加速。I/O 是 ready 至 sync 的本机单设备 cgroup 计数、内存是全过程累计峰值并包含采样子进程，不是官方资源口径。

## 正确性验证

- 新增 `tests/test_vector_pk_filter.py`：稀疏及负主键、多种范围操作符、距离并列、空集、混合条件回退、删除/回滚、向量更新、过滤预算不足时回退。
- Docker 256 MiB `python -m unittest discover -v`：159 项通过，`.local-pkfilter-unittest.log`。
- 官方公开 JSONL 用例：20/20 通过，`.local-pkfilter-public-20261127/result.json`。
- Docker 64 MiB 生命周期测试：失败语句原子性、提交后 SIGKILL、未提交回滚及重启通过，`.local-pkfilter-lifecycle.json`。
- 三轮第一版及一轮最终版 64 MiB 大数据压力测试均通过、无 OOM/OOM kill。
- 非隐藏功能准入，不代表平台成绩或完全覆盖所有竞争条件。

源码 `vector_index.py` 与新增测试的 SHA-256 已同步 `docs/source-manifest.json` 和 `agent/conversation.json`；`tests.validate_submission` 通过（59 个文件）。材料 130,952 字节，距 128 KiB 限制 120 字节，不能无整理地继续加入内容。用户之前的未提交学习材料保留；当前改动均未提交/推送，正式高分版本未改变。
