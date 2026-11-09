# MiniVectorDB

自研的单进程轻量向量数据库，使用 Python 标准库实现 SQL、磁盘存储和事务，并提供自行编写的 C++ 精确 TopK 内核。没有使用现成数据库引擎或向量索引库。

**当前已通过 Linux 容器验证和首批公开 SQL 样例，尚未完成正式评测协议适配。** 起始于官方 `c842a368c3394748d0499386c18f6b465242e141`，现已快进合入 `a3dba559c1e7ebdc48bc990ffb1a4c75d1c1075c` 的公开用例。`mysqltest/README.md` 明确这些用例不包含完整驱动，也不代表新的运行协议发布。

## 启动与测试

要求 Python 3.11 或更高版本，无第三方 Python 依赖。启用原生加速需要支持 C++17 的 g++；未编译内核时自动使用完整 Python 执行路径。已验证 Windows x86_64（Python 3.11.9、MinGW g++ 8.1.0）和 Linux x86_64（Python 3.11.18、GCC 12.2.0）。

```bash
python -m minivectordb.build_native
python -m minivectordb --data-dir .local-data
python -m unittest discover -v
python -m tests.benchmark --output .local-benchmark.json
```

启动后输入以分号结尾的 SQL。EOF 正常退出并回滚未提交事务。Linux 下 SIGTERM 正常关闭，SIGKILL 用于强制终止。参数 `--mode exact` 为默认；`--mode ann` 当前仍使用精确扫描回退。`--disable-native` 可强制使用 Python 路径进行对照。

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

Linux 镜像已构建并验证：64 项测试通过，其中首批公开 5 组、32 条 SQL 分别经类型化执行器和本地 stdin/stdout 适配器核对；真实容器强杀、EOF 和 SIGTERM 恢复通过。在 64 MiB、无 swap 限制下，164,694,642 字节 WAL 完成恢复与 20,000 行外部排序验证，无 OOM。这些结果不等于官方协议预检或正式准入。完整命令及局限见 [容器验证](docs/docker-validation.md)。

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
| 向量索引 | 磁盘连续 float32 数据与主键映射；自研 C++ 精确 TopK；无 ANN 图结构 |
| 协议 | 临时 TSV 开发适配器，与核心执行器分离；正式接口待题包确认 |

不支持的 JOIN、NULL、聚合、子查询、ALTER TABLE、外键、嵌套事务、主键更新等返回错误，不会按其他语句静默执行。

## 本地协议

本节是项目自测约定，**不是官方 round2 协议**。正式题包到位后主要替换 `minivectordb/protocol.py` 和入口参数。

- 输入：UTF-8 SQL，分号分隔，支持多行和单引号内分号；单引号用两个单引号转义。支持 `--`、`#`、`/* ... */` 注释。
- 就绪：恢复完成后输出 `READY` 并刷新。
- 成功：`OK<TAB>受影响行数`。
- 查询：`RESULT<TAB>列名...`，随后每行一个 TSV 记录，最后 `END<TAB>行数`。
- 错误：`ERROR<TAB>错误类别<TAB>说明`，失败后可继续发送下一语句。
- 字符串输出转义反斜杠、Tab、换行和回车；向量输出为紧凑数组文本。
- 日志与致命 I/O 错误写 stderr；存储故障终止进程，不在潜在已持久化提交后继续报告普通失败。

临时错误类别包括 `SYNTAX_ERROR`、`TYPE_ERROR`、`INVALID_VECTOR`、`CONSTRAINT_ERROR`、`NOT_FOUND`、`TRANSACTION_ERROR`、`UNSUPPORTED`。官方错误码待替换。

## 文件与内存

所有数据库数据、主键索引、事务暂存和排序临时文件位于 `--data-dir`，容器默认为 `/data`。

| 文件 | 用途 |
| --- | --- |
| database.wal | 权威数据源，包含表定义、索引定义及已提交行版本 |
| primary.idx | 可重建的磁盘哈希主键索引 |
| transaction.stage | 当前事务追加暂存，不计入已提交状态 |
| transaction.idx | 当前事务的磁盘哈希覆盖索引 |
| sort-*.tmp | 排序归并临时文件 |
| modify-*.tmp | UPDATE／DELETE 的语句候选记录暂存 |
| checkpoint.wal | 原子发布前的压缩日志临时文件 |
| flat-*.vec、flat-*.keys | 可重建的连续向量和主键文件，原生索引使用 mmap 读取 |

`--sort-memory-mib` 默认 4，控制排序缓冲的保守估算预算；`--cache-memory-mib` 默认 16，限制不可变已提交记录的 LRU 缓存，设为 0 可禁用。它们不是整个进程的硬内存限额。表目录、输入 SQL／AST、单条记录及 Python 运行时仍占内存；mmap 索引与文件页缓存还需由正式 cgroup 测量。

提交后，日志达到 `--checkpoint-mib`（默认 64 MiB）且超过有效行记录大小的三倍时，执行流式日志压缩。压缩写入已同步的临时 WAL，再原子替换，重建索引并清空旧位置缓存。压缩期间需要同时容纳旧日志和新快照，准备空间仍须对照正式磁盘限制。

建向量索引时生成派生文件；重启时在 READY 前从提交数据重建。数据或普通列过滤值变化时索引与过滤缓存失效，随后按当前事务可见状态重建。过滤缓存只保存候选位置，限制为排序预算的一半且不超过 1 MiB；构建新过滤缓存时临时分配也有相同上限。向量本体通过磁盘映射读取，页缓存仍计入正式内存统计。

原生路径处理单个 L2_DISTANCE 升序和有界 LIMIT。它保留 K+1 个候选、检查数值边界，并用 Python math.dist 复算返回记录；并列或过近的第 K／K+1 距离、过大的 K、超出过滤缓存预算、维度超过 65,536 或未编译内核时，使用通用精确执行器。这里的边界仅决定执行路径，不限制 SQL 功能范围。

## 当前限制与下一步

1. 正式协议、完整语法和资源边界尚缺；DDL 在显式事务中可回滚、无活动事务的 COMMIT／ROLLBACK 报错、索引名全局唯一是当前本地约定，待正式题包核对。
2. 向量索引为自研精确 flat 扫描，尚无 ANN 算法；两种模式目前结果相同。高频更新可能反复重建派生文件，仍需优化增量维护。
3. 日志压缩为同步操作，触发时增加该次提交延迟，临时占用旧日志加新快照的空间；阈值尚需正式负载调优。
4. 删除槽位在日志压缩时回收，压缩之前仍占索引空间。真实峰值内存、磁盘容量和恢复超时尚待正式环境验证。
5. 测试覆盖进程终止，不承诺断电、介质损坏或外部篡改恢复。内部序列化只读取程序自己写入的数据目录。
6. 同一数据目录只允许一个数据库进程使用；本题不提供多进程写入协调。
7. 本地结果不代表功能准入、性能排名或正式容器预检通过。

参见 [设计说明](docs/design.md)、[验证记录](docs/validation.md)、[赛题要求](docs/minivectordb-requirements.md)。真实交互与结果摘要保存在 `agent/conversation.json`，不将 Agent 自动实现伪装成学生理解证据。

本轮交付状态见 [提交清单](docs/submission.md)，技术取舍和验证中发现的问题见 [复盘](docs/retrospective.md)。
