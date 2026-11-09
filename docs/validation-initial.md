# MiniVectorDB 本地验证记录

验证日期：2026 年 11 月 9 日。环境：Windows x86_64、Python 3.11.9。代码位于官方 round2 基础提交之上的未提交工作区，文件摘要见 source-manifest.json。

## 正确性与恢复

命令：`python -m unittest discover -v`。

实际结果：**37 项测试通过，耗时 24.233 秒**。完整输出保存在 [validation-tests.txt](validation-tests.txt)。

覆盖类型和 Unicode 边界、float32 舍入与非法向量、多行语句原子性、事务失败后的继续提交、读自身修改、删除重插、普通排序与外部归并、过滤 TopK、随机参考执行器对照、索引定义持久化及缓存隔离。

恢复覆盖已确认提交后强杀、未提交事务强杀、EOF 回滚、提交尾部写入前终止、同步后响应前终止、恢复过程中连续三次终止，以及未确认提交帧的每个字节前缀。checkpoint 在原子替换前后终止均通过恢复检查；模拟单次写入最多 3 字节的测试确认短写不会导致确认提交丢失。

磁盘工作集测试使用 4096 条、每条 8192 字符的数据，WAL 大于 32 MiB；设置排序和记录缓存各 256 KiB，恢复并完整倒序读取时，tracemalloc 跟踪的 Python 分配峰值低于 8 MiB。该指标不包含完整运行时、mmap 和系统文件页缓存，不能替代 cgroup memory.peak。

## 本地端到端 benchmark

本地自建均匀随机数据，种子 20261109，10,000 条、128 维、每批 100 条自动提交、K=10、一半查询带过滤。它不是尚未取得的官方数据生成器。

| 开发阶段 | 查询数 | 写入秒数 | 查询秒数 | 重启至 READY 秒数 | Windows 峰值工作集 MiB |
| --- | --- | --- | --- | --- | --- |
| 初版无记录缓存 | 1000 | 8.124969 | 143.595392 | 0.255641 | 26.02 |
| 仅增加记录缓存 | 1000 | 20.040442 | 214.024789 | 0.288094 | 29.41 |
| 表达式预绑定及日志压缩 | 1000 | 23.446918 | 84.886694 | 0.276945 | 29.86 |
| 最终源码短写修复后冒烟 | 100 | 21.887446 | 9.133953 | 0.293676 | 29.69 |

所有上述运行均核对返回 ID、顺序、距离，过滤及无过滤两组 Recall@10 均为 1.0，并在重启后逐条核对 ID、标签和向量内容。各轮 WAL 均为 6,202,671 字节，这只是文件大小，**不是物理 I/O 指标 B**。

前三轮是开发中间版本、每个版本的一次本地测量，没有按冻结提交保存完整源码摘要；最后一轮为最终源码的 100 次查询冒烟测试，记录了每个核心源码文件 SHA256，已与当前文件核对。100 次与 1,000 次的查询时间不能直接相除当作优化倍数。

各阶段不是同一版本的三个正式测量轮次，不计算 E 或赛事排名。写入时间存在明显波动，早期测量还与部分开发验证重叠，不能将差异完全归因于缓存。缓存本身没有在中间轮次证明提速；表达式剖析显示每行重复哈希向量表达式是热点，将表达式编号预先绑定后，后续 1,000 次查询实测为 84.886694 秒，但仍需受控多轮复测确认收益。

- [benchmark-before-cache.json](benchmark-before-cache.json)
- [benchmark-with-cache.json](benchmark-with-cache.json)
- [benchmark-expression-hoist.json](benchmark-expression-hoist.json)
- [benchmark-current-smoke.json](benchmark-current-smoke.json)

复现命令：

```bash
python -m tests.benchmark --output .local-benchmark.json
python -m tests.benchmark --queries 100 --output .local-smoke-benchmark.json
python -m tests.validate_submission
```

benchmark 的 T 包含本地管道、SQL 和输出开销，写入还包含驱动端 SQL 生成；进程峰值工作集不等于容器峰值内存。没有采集正式数据设备 I/O，也没有禁用 swap 或建立 Linux cgroup 资源限额，不能据此生成正式性能分数。

## 交付检查与未完成项目

- `python -m compileall -q minivectordb tests` 已通过。
- `git diff --check` 已通过，Git 提示 Dockerfile 后续可能转换 LF/CRLF，不是语法错误。
- 学习材料为单个 agent/conversation.json，记录真实用户发言与工具结果，学生未提供的技术解释不由 Agent 代写。
- 材料检查器验证 UTF-8、顶层对象、128 KiB 上限、重复键、非标准数值和源码摘要；不检查官方协议。
- 官方基础分支当前没有题包驱动、错误码或明确输出格式；当前 TSV 协议只是本地开发约定。
- 本机 Docker Linux 引擎管道不可用；WSL2 返回 HCS_E_HYPERV_NOT_INSTALLED。因此 Dockerfile 已提供，但 Linux 构建、容器运行及 cgroup 检查尚未执行。
- ANN 仍为精确 flat 回退，没有自研近似加速结构；必须根据正式题包确认必做范围和性能预算。
- 没有提交正式评测，没有功能准入或排名结果，也没有可用于正式提测的新 commit SHA。
