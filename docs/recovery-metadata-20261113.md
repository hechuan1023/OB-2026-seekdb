# 恢复期只扫描行元数据

日期：2026-11-13

## 改动

数据库启动恢复时，完整 WAL 仍会先校验事务校验和，再逐条重建主键索引和
`live_bytes`。候选版本新增 `records_metadata()`：对紧凑行只解析记录类型、
表号、主键、删除标志和值字段边界，不再构造完整 Python 行、字符串或
`array('f')` 向量。旧 pickle 行仍走原有完整解码路径，因此既有数据目录
格式保持兼容。

`_apply()` 新增内部 `rowmeta` 分支。它只更新恢复所需的派生主键索引和存活
字节数，处理已删除记录、重复更新和最终存活状态的方式与完整行解码一致。
实际 SQL 读取仍通过原有 `read_record()` 解码，不改变查询语义。

另外两处改动只移除确定不会产生结果的工作：

- 小端主机上的 `DiskHash.entries()` 直接遍历已有 `memoryview`，避免每个
  空槽调用一次 `Struct.unpack_from`。
- 没有活动事务时，`Storage.get()` 和 `Storage.rows()` 不再探测空的
  transaction overlay。

WAL 格式、事务提交点、`fsync` 顺序、恢复前 READY 条件和查询结果均未改变。

## 恢复测量

隔离恢复探针使用固定数据目录，重复执行 5 对基线/候选启动：

| 指标 | 基线 | 候选 |
| --- | ---: | ---: |
| reopen 中位耗时 | 0.0642 s | 0.0617 s |
| 首次 reopen 配对中位比 | 1 | 0.93468 |
| 平均 reopen 耗时 | 0.06697 s | 0.06254 s |

`DiskHash.entries()` 微基准从约 2372.7 µs 降到 1325.3 µs。

完整启动 AB/BA 同时受到快照重建和宿主调度影响，配对中位比分别为
`1.0590` 和 `0.99818`，方向不稳定。因此本报告只把隔离恢复探针作为该改动
的主证据；完整启动结果不支持宣称稳定的端到端启动提速。

本轮改动不进入查询距离计算或提交写入热路径。端到端查询和写入仍做了
AB/BA 回测，但执行顺序造成的宿主偏差大于预期收益，未观察到明确的代码级
回退。所有回测结果均逐条核对，不能把本地 Docker Desktop 数据解释为官方
T/B/M、E 或正式排名结果。

## 正确性与资源验证

候选镜像：

```text
minivectordb:recovery-skip-overlay-20261113
sha256:849431d960a3a7efd67aec81d8cdaf76edbbae18f98f9ff69f0bebeb3322bc37
```

- Windows：`186/186` 项测试通过。
- Linux Docker，2 CPU、256 MiB：`186/186` 项测试通过。
- 官方公开 JSONL：`20/20` 通过。
- 64 MiB 生命周期：`4/4` 通过，覆盖 SIGKILL、SIGTERM、EOF 和同卷重启。
- 新增测试
  `test_recovery_index_scan_does_not_decode_row_payloads` 在恢复期间禁用
  `_decode_row`，确认紧凑行仍能恢复 schema 和主键索引。
- 64 MiB 超内存压力使用 18,000 行、1,024 维，向量文件
  `73,728,000 B`，大于 64 MiB；`oom=0`、`oom_kill=0`，峰值
  `67,108,864 B`，耗时约 `11.518 s`。

## 结论与边界

保留该改动。它减少恢复阶段的对象分配和 Python 解码工作，同时保留完整 WAL
校验、旧格式兼容以及 READY 前索引重建。隔离恢复测量方向一致，但完整启动
时间的宿主噪声较大；正式性能影响仍需以平台统一 T/B/M 测量为准。

本轮还测试并撤回了快照顺序读取、WAL `fallocate` 预留、常驻原生线程池和
四候选 AVX2 精确距离复核，原因见
[optimization-rejections-20261113.md](optimization-rejections-20261113.md)。
