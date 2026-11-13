# MiniVectorDB

**2026-11-10 协议更新：**官方 `f4d4e26653af823a7601668802f80e374ad6643c` 发布 JSONL 协议与公开检查器。Docker 默认启动 JSONL 接口，详情见 [官方协议](STDIO_PROTOCOL.md)。本地命令 `python -m minivectordb` 默认仍使用 SQL 自测接口；可通过 `--wire jsonl` 切换。

自研的单进程轻量向量数据库，使用 Python 标准库实现 SQL、磁盘存储和事务，并提供自行编写的 C++ 精确 TopK 内核。没有使用现成数据库引擎或向量索引库。

**已按官方 JSONL 协议适配 Docker 入口，官方检查器的握手和 20 组公开用例通过。** 此前用户反馈正式提测未通过且没有详细日志；旧版本握手与现公布协议不兼容，但缺少平台日志，不能确认当时失败的确切原因。本次尚未进行正式提测。

## 启动与测试

要求 Python 3.11 或更高版本，无第三方 Python 依赖。启用原生加速需要支持 C++17 的 g++；未编译内核时自动使用完整 Python 执行路径。已验证 Windows x86_64（Python 3.11.9、MinGW g++ 8.1.0）和 Linux x86_64（Python 3.11.18、GCC 12.2.0）。

```bash
python -m minivectordb.build_native
python -m minivectordb --data-dir .local-data
python -m unittest discover -v
python -m tests.run_public_cases --output .local-public-v4.json
python -m tests.benchmark --output .local-benchmark.json
```

启动后输入以分号结尾的 SQL。EOF 正常退出并回滚未提交事务。Linux 下 SIGTERM 正常关闭，SIGKILL 用于强制终止。参数 `--mode exact` 为默认；`--mode ann` 在基准形状使用自研 SQ8 候选过滤并以精确内核复核，不满足适用条件时回退到精确扫描。`--disable-native` 可强制使用 Python 路径进行对照。

```sql
CREATE TABLE docs (
    id BIGINT PRIMARY KEY,
    category VARCHAR(32),
    content VARCHAR(1024),
    embedding VECTOR(4)
);
INSERT INTO docs VALUES
    (1, 'database', '事务与恢复', '[1,0,0,0]'),
    (2, 'database', '向量索引', '[0,1,0,0]'),
    (3, 'travel', '旅行攻略', '[0,0,1,0]');
SELECT id, content FROM docs WHERE category='database' ORDER BY id;
SELECT id, L2_DISTANCE(embedding, '[0.9,0.1,0,0]') AS distance
FROM docs WHERE category='database' ORDER BY distance LIMIT 2;

BEGIN;
UPDATE docs SET embedding='[0.8,0.2,0,0]' WHERE id=1;
DELETE FROM docs WHERE id=2;
ROLLBACK;

BEGIN;
UPDATE docs SET content='已提交的内容' WHERE id=1;
COMMIT;
CREATE VECTOR INDEX docs_vector ON docs(embedding);
```

结束输入后，再以同一 `--data-dir` 启动，执行 `SELECT * FROM docs ORDER BY id;` 核对持久化内容。第二次启动不需要重新建表。

## Linux 容器

```bash
docker build -t minivectordb:dev .
docker run --rm -i -v minivectordb-data:/data minivectordb:dev
```

Dockerfile 用固定 SHA256 digest 的 Python 3.11 Bookworm 镜像进行多阶段构建，编译阶段的 32 项 Debian 包版本记录在 `build-dependencies.lock`。源码编译得到自研 C++ 共享库；编译器和本地预编译文件不进入最终镜像，运行时无需联网。依赖版本下架时构建应失败，不能静默换版；正式允许的镜像和依赖范围仍待确认。

当前版本 Windows 与 Linux 容器完整回归均为 183 项通过；官方检查器针对实际 Docker 入口的握手与 20 组公开用例通过，详见 [JSONL 验证记录](docs/official-jsonl-validation.json)。64 MiB 超内存压力测试通过，`oom_kill` 为 0。当前 `perf/iterative-optimization` 测试分支包含：内核距离归约内联与 FMA 运行时选择、按 cgroup CPU 配额启用的无过滤大表及大过滤集 exact TopK、小 K 有序候选收集、TopK 结果物化与 JSONL 小结果单次写、规范多行 INSERT 解析快路径、高基数列值缓存与 1.5 MiB 过滤预算、原生及按 cgroup CPU 配额并行的批量向量解析、小数据集内存快照、按 cgroup 限额选择匿名主键索引、干净 WAL 启动不额外同步、小事务内存暂存／大事务溢写，以及 ANN 模式预计算行范数的 SQ8 点积候选扫描。本地对照见 [OpenMP exact TopK 记录](docs/openmp-exact-20261113.md)、[OpenMP 批量向量解析记录](docs/openmp-vector-parse-20261113.md)、[ANN 模式验证记录](docs/ann-mode-validation-20261113.md)、[SQ8 点积候选记录](docs/sq8-dot-20261113.md)、[事务暂存记录](docs/transaction-stage-memory-20261113.md)、[TopK 结果物化记录](docs/python-topk-materialization-20261113.md)、[JSONL 输出合并记录](docs/json-output-batching-20261113.md)、[INSERT 快路径记录](docs/insert-fast-path-20261113.md)、[小 K 优化记录](docs/sorted-small-k-20261113.md)、[干净 WAL 恢复记录](docs/clean-wal-recovery-20261113.md) 与 [过滤缓存优化记录](docs/jsonl-filter-optimization.md)。正式分支 `2026-competition-round2` 未修改。历史 v4 验证、旧版本的容器压力测试和测量分别见 [v4 验证与复盘](docs/public-v4-validation.md)、[容器验证](docs/docker-validation.md) 与 [优化记录](docs/r3-optimization.md)。这些结果不等于正式准入。

## 功能状态

| 能力 | 当前实现 |
| --- | --- |
| SQL | CREATE TABLE、INSERT、多行 INSERT、SELECT、UPDATE、DELETE |
| 类型 | BIGINT、按 Unicode 字符计长的 VARCHAR、固定维度 float32 VECTOR |
| 查询 | 投影、SELECT *、比较、AND、多排序键、ASC／DESC、LIMIT |
| 向量 | L2_DISTANCE、精确 TopK、先过滤再 TopK、距离并列按主键升序 |
| 事务 | 自动提交、BEGIN／COMMIT／ROLLBACK、读自身修改、语句级失败回滚 |
| 恢复 | 带长度和校验和的提交日志；重建磁盘索引；丢弃不完整尾部 |
| 大结果 | 流式扫描；有限内存排序和磁盘归并；TopK 剪枝 |
| 向量索引 | 磁盘连续 float32 数据与主键映射；自研 C++ 精确 TopK；大表无过滤查询及至少 768 行的过滤集按 cgroup CPU 配额并行；显式 ANN 使用自研 SQ8 候选过滤并精确复核，无外部 ANN 图库 |
| 协议 | Docker 默认官方 JSONL；本地另保留 TSV 开发适配器 |

不支持的 JOIN、NULL、聚合、子查询、ALTER TABLE、外键、嵌套事务、主键更新等返回错误，不会按其他语句静默执行。

## 本地协议

本节是本地 SQL 自测约定，**不是官方 round2 协议**。Docker 默认接口见 [官方协议](STDIO_PROTOCOL.md)；本地进程可用 `--wire jsonl` 启动同一接口。

- 输入：UTF-8 SQL，分号分隔，支持多行和单引号内分号；单引号用两个单引号转义。支持 `--`、`#`、`/* ... */` 注释。
- 就绪：恢复完成后输出 `READY` 并刷新。
- 成功：`OK<TAB>受影响行数`。
- 查询：`RESULT<TAB>列名...`，随后每行一个 TSV 记录，最后 `END<TAB>行数`。
- 错误：`ERROR<TAB>错误类别<TAB>说明`，失败后可继续发送下一语句。
- 字符串输出转义反斜杠、Tab、换行和回车；向量输出为紧凑数组文本。
- `--local-mode-controls` 是仅用于本地自测的显式开关，允许在完整 SQL 之间发送独立的 `--mode exact` / `--mode ann` 行，返回 `OK<TAB>0`；切换保留进程及事务，默认关闭。它不是官方模式传输约定。题包的 `--restart` 由测试适配器关闭并重新启动进程，不发送给 SQL 解析器。
- 日志与致命 I/O 错误写 stderr；存储故障终止进程，不在潜在已持久化提交后继续报告普通失败。

临时错误类别包括 `SYNTAX_ERROR`、`TYPE_ERROR`、`INVALID_VECTOR`、`CONSTRAINT_ERROR`、`NOT_FOUND`、`TRANSACTION_ERROR`、`UNSUPPORTED`。官方错误码待替换。

## 文件与内存

所有数据库数据、主键索引、事务暂存和排序临时文件位于 `--data-dir`，容器默认为 `/data`。

| 文件 | 用途 |
| --- | --- |
| database.wal | 权威数据源，包含表定义、索引定义及已提交行版本 |
| primary.idx | 可重建的哈希主键索引；cgroup 限额至少 128 MiB 时改用匿名内存，不写此文件 |
| transaction-stage-* | 大事务溢写暂存；小事务仅在内存中暂存，不计入已提交状态 |
| transaction.idx | 当前事务的磁盘哈希覆盖索引 |
| sort-*.tmp | 排序归并临时文件 |
| modify-*.tmp | UPDATE／DELETE 的语句候选记录暂存 |
| checkpoint.wal | 原子发布前的压缩日志临时文件 |
| flat-*.vec、flat-*.keys | 可重建的连续向量和主键文件；小数据集直接驻留内存，大数据集写文件并由原生索引 mmap 读取 |

`--sort-memory-mib` 默认 4，控制排序缓冲的保守估算预算；`--cache-memory-mib` 默认 4，限制不可变已提交记录的 LRU 缓存，设为 0 可禁用。它们不是整个进程的硬内存限额。表目录、输入 SQL／AST、单条记录及 Python 运行时仍占内存；mmap 索引与文件页缓存还需由正式 cgroup 测量。

提交后，日志达到 `--checkpoint-mib`（默认 64 MiB）且超过有效行记录大小的三倍时，执行流式日志压缩。压缩写入已同步的临时 WAL，再原子替换，重建索引并清空旧位置缓存。压缩期间需要同时容纳旧日志和新快照，准备空间仍须对照正式磁盘限制。

建向量索引时生成派生文件；重启时在 READY 前从提交数据重建。成功语句实际改变向量或记录集合时，清除对应表的快照和过滤缓存；只改变普通列时保留向量文件，清除依赖这些列的过滤缓存。失败语句、零行修改、相同值更新和只读事务保留有效快照。COMMIT 保留当前可见快照；ROLLBACK 按该事务实际修改的表和列失效，随后按恢复的可见状态重建。过滤缓存保存候选位置，并可为单列等值过滤保留按快照顺序排列的列值列表；两者共用同一预算，限制为排序预算的一半且不超过 1.5 MiB，超限按 LRU 淘汰。事务暂存最多在内存保留 512 KiB，超过后溢写到 `/data`；提交时仍完整写入并同步 WAL。向量本体通过磁盘映射读取，页缓存仍计入总体内存。

原生路径处理单个 L2_DISTANCE 升序和有界 LIMIT。它保留 K+1 个候选、检查数值边界，并用 Python math.dist 复算返回记录；无过滤大表或至少 768 行的过滤集在 cgroup CPU 配额大于 1 时按区间并行扫描，小过滤集和单 CPU 环境保持单线程。并列或过近的第 K／K+1 距离、过大的 K、超出过滤缓存预算、维度超过 65,536 或未编译内核时，使用通用精确执行器。这里的边界仅决定执行路径，不限制 SQL 功能范围。

## 当前限制与下一步

1. 已取得 JSONL 协议及公开驱动；完整语法、错误码映射和正式资源边界仍待核对。DDL 在显式事务中可回滚、无活动事务的 COMMIT／ROLLBACK 报错、索引名全局唯一是当前本地约定。
2. `exact` 使用自研精确 flat 扫描；`ann` 使用自研 SQ8 候选过滤并精确复核，候选最终仍满足精确结果检查。真实向量更新和记录增删会失效并重建派生 SQ8 与 flat 数据；目前没有实现增量 ANN 维护。
3. 日志压缩为同步操作，触发时增加该次提交延迟，临时占用旧日志加新快照的空间；阈值尚需正式负载调优。
4. 删除槽位在日志压缩时回收，压缩之前仍占索引空间。真实峰值内存、磁盘容量和恢复超时尚待正式环境验证。
5. 测试覆盖进程终止，不承诺断电、介质损坏或外部篡改恢复。内部序列化只读取程序自己写入的数据目录。
6. 同一数据目录只允许一个数据库进程使用；本题不提供多进程写入协调。
7. 本地结果不代表功能准入、性能排名或正式容器预检通过。

参见 [设计说明](docs/design.md)、[验证记录](docs/validation.md)、[赛题要求](docs/minivectordb-requirements.md)。真实交互与结果摘要保存在 `agent/conversation.json`，不将 Agent 自动实现伪装成学生理解证据。

本轮交付状态见 [提交清单](docs/submission.md)，技术取舍和验证中发现的问题见 [复盘](docs/retrospective.md)。
