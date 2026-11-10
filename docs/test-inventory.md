# 测试用例总览：官方公开题包与独立交叉验证组

更新：2026-11-10。本文汇总本项目全部测试资产：官方公开题包（`mysqltest/`，未修改）、仓库自测套件（`tests/`，106 项）与独立交叉验证组（`tests_cross/`，六组）。所有套件在撰写时全部通过；历史证据见 [交叉验证报告](cross-validation-report.md) 与 [优化记录](cross-validation-optimization.md)。

## 1. 分层总览

| 层 | 位置 | 作者 | 数量 | 说明 |
| --- | --- | --- | --- | --- |
| 官方公开题包 | `mysqltest/` | 组织方（`9f6df2ba` 原样合入） | 20 组 / 95 条 SQL | 断言权威；不得修改来迁就实现 |
| 仓库自测套件 | `tests/` | 原开发 Agent | 106 项 | 引擎/索引/恢复/题包适配的 unittest |
| 独立交叉验证组 | `tests_cross/` | 交叉验证 Agent（未参与开发） | 8 组，约 1,450 次验证动作 | 自带管道客户端，真值独立建立 |

正式准入共 29 个用例：公开 20 个之外另有 9 个隐藏（输入与预期由组织方保留）。公开样例通过不等于正式准入。

## 2. 官方公开题包（mysqltest/，minivectordb-public-v4）

### 2.1 断言格式

- `.test` 文件每个非空行是一条完整 SQL 或驱动指令；同名 `.result` 是 JSON 数组，与 SQL 按顺序一一对应（驱动指令不占断言位）。
- `{}`：该 SQL 必须成功；`error`：必须失败且错误语义名精确匹配（`duplicate_key`/`dimension_mismatch`/`unsupported_sql`），错误后继续执行；`affected_rows` 精确比较；`columns` 含顺序；`rows` 按序比较（`unordered: true` 时按多重集合，重复行不丢）；浮点绝对/相对容差均为 `1e-5`。
- 驱动指令：`--restart`（正常关闭回滚未提交事务后同目录重启）、`--mode ann`（后续向量请求 ann，每组初始 exact）。
- 每组从独立空目录开始。执行路径：类型化执行器与本地 stdin/stdout 适配器各跑一遍，共 40 项检查。

### 2.2 用例清单（按 manifest 顺序）

| # | 用例 | SQL 数 | 类别 | 检查要点 |
| --- | --- | --- | --- | --- |
| 1 | sql_crud | 7 | sql | 单记录增删改查，中文内容 |
| 2 | sql_comparisons | 4 | sql | `>=`/`<` 组合 AND、等值 |
| 3 | sql_ordering | 4 | sql | 升/降序与 LIMIT |
| 4 | sql_empty | 3 | sql | 空表查询、空表删除 |
| 5 | sql_primary_key | 4 | sql | 重复主键报 `duplicate_key`，原记录保留，错误后继续 |
| 6 | sql_unicode | 3 | sql | 中文写入读出 |
| 7 | sql_bigint | 3 | sql | ±9007199254740993 精确读写（超 double 安全整数） |
| 8 | sql_unsupported | 3 | sql | ALTER TABLE 报 `unsupported_sql` |
| 9 | storage_restart | 3 | storage | `--restart` 后数据保留 |
| 10 | vector_euclidean | 3 | vector | `[3,4]` 与 `[0,0]` 距离 = 5（欧氏距离非平方和） |
| 11 | transaction_commit | 6 | transaction | BEGIN→UPDATE→COMMIT 可见 |
| 12 | transaction_rollback | 6 | transaction | ROLLBACK 恢复 |
| 13 | storage_mutations | 6 | storage | 更新/删除/向量修改混合 + 重启一致 |
| 14 | storage_multiple_tables | 6 | storage | 多表定义与记录持久化 |
| 15 | vector_ties_and_filter | 5 | vector | **先过滤后 TopK**、距离并列按主键升序、过滤空集合、K 超候选数 |
| 16 | vector_float32 | 3 | vector | 16777217 float32 舍入语义 |
| 17 | vector_index_maintenance | 9 | vector | 建索引后增删改、`--restart`、`--mode ann` 三态结果一致 |
| 18 | transaction_failed_insert | 8 | transaction | 事务内多行 INSERT 尾行重复 → 整条无残留，先前修改可提交 |
| 19 | transaction_autocommit_atomic | 4 | transaction | 自动提交多行 INSERT 维度错误 → 整条无残留 |
| 20 | transaction_shutdown_rollback | 5 | transaction | BEGIN→DELETE 全表→`--restart` → 未提交回滚 |

统计：`{}` 断言 29 个、错误断言 4 个（`duplicate_key`×2、`dimension_mismatch`、`unsupported_sql`）、7 次 `--restart`、1 次 `--mode ann`。公开包未覆盖 NaN/Infinity、float32 溢出、强杀恢复、恢复途中再杀、超内存数据与 ANN 召回率——这些在正式要求内，由第 4 节独立组与仓库套件补位。

### 2.3 运行方式

```bash
# 两条执行路径（typed + stdio），40 项检查并输出结构化报告
python -m tests.run_public_cases --output .local-public.json
python -m unittest tests.test_public_cases -v

# Linux 容器内（镜像实现 + 只读挂载测试与题包）
docker run --rm --network none --cpus=2 --memory=256m --memory-swap=256m \
  -e TMPDIR=/data -v "$PWD/tests:/app/tests:ro" -v "$PWD/mysqltest:/app/mysqltest:ro" \
  --entrypoint python minivectordb:cross-review -m tests.run_public_cases --output /results/public.json
```

## 3. 仓库自测套件（tests/，106 项）

`python -m unittest discover -v`；无第三方依赖；原生内核缺失时自动跳过原生路径（跳过不能记为原生通过）。构成：引擎与 SQL 语义（`test_engine.py`）、向量索引与生命周期（`test_vector_index.py`、`test_index_lifecycle.py`）、恢复与协议（`test_recovery.py`，含提交中断、逐字节 WAL 尾部、恢复中再终止、checkpoint 替换前后终止）、题包适配与防误判（`test_public_cases.py`、`test_public_adapter.py`）、内存探针（`test_memory.py`）。另有容器级驱动：`docker_validation.py`（真实入口 SIGKILL/EOF/SIGTERM/同卷）、`container_vector_pressure.py`（18,000×1024 维 / 64 MiB）、`container_benchmark.py`（三轮 T/B/M 形状测量）。

## 4. 独立交叉验证组（tests_cross/，六组）

均为自带最小管道客户端，不 import 被测仓库的测试工具；真值独立建立（float32 用 `struct.pack/unpack('f')`，距离用 Python `math.dist`，排序键 `(distance, primary_key)`），不复用被测实现的距离、过滤或排序函数。

### 4.1 p0_transaction_kill.py — 事务边界与强杀时序（19 项）

- 交接文档指定的四分支场景：事务内失败语句原子性 → COMMIT+EOF / ROLLBACK+EOF / **读到 COMMIT 成功响应后 4.6ms 立即 SIGKILL** / 未 COMMIT 直接 SIGKILL，各自重启核对。
- 迁移反例：自动提交多行失败无残留、事务内向量维度错误后仍可提交、多行 UPDATE 中途 VARCHAR 超长整条回滚。
- 要点：等明确响应后才 kill、并发读 stderr、独立数据目录。

### 4.2 p0_vector_modes.py — 模式切换、缓存一致性与独立真值（180 项）

- 事务中 `--mode ann/exact` 控制行切换（进程不替换、事务不丢、可提交可回滚）。
- 过滤缓存刷新：改过滤列 / 零行修改 / 相同值修改 / 失败语句之后。
- 事务内交错增删改 × COMMIT/ROLLBACK 双分支 + 重启核对。
- 300 行随机数据：native 与 `--disable-native` 双路径对照独立真值。
- 边界：等距并列、N=0、N<K、K=0、LIMIT 边界、普通 ORDER BY 并列主键升序、float32 舍入语义、float32 极值（3e38）、近似/精确并列、非法向量拒绝、DDL 回滚等本地约定。

### 4.3 p1_recovery_timing.py — 恢复时序矩阵（22 项）

- T1 提交请求前强杀 → 完整回滚；T2 提交处理中终止（`after_commit_sync`）→ 允许的完整生效；T2b footer 前终止 → 允许的完整未生效；T3 自动提交确认后强杀 → 保留。
- T4 恢复途中连续两次强杀，第三次启动完整恢复并可继续写入。
- T5 脏向量快照重建途中强杀（`during_vector_rebuild`）→ 恢复正确、查询正常。
- T6 checkpoint 压缩发布前/后强杀 → 已提交删除保留、残留 `checkpoint.wal` 被启动清理。
- 说明：failpoint 为被测实现自带的测试注入点，断言与真值独立。

### 4.4 p2_sql_surface.py — SQL 表面、类型边界与协议行为（166 项，host + 容器各一遍）

- 解析器：`--`/`#`/`/* */` 注释、串内分号、`''` 转义、多行 SQL、反引号标识符、混合大小写、`BEGIN TRANSACTION`、`COMMIT WORK` 报错、注释行无响应、未终止字符串/块注释在 EOF 报 `SYNTAX_ERROR` 后正常退出。
- 类型：BIGINT ±2^63 边界与前导零；VARCHAR 码点计长；码点比较与排序。
- 非法向量全谱：NaN/Infinity/溢出/维度/空数组/布尔/嵌套/JSON 语法错/引号嵌套。
- 不支持语法必须报错：OR/IN/NOT/聚合/JOIN/NULL/主键更新/重复赋值/别名规则/`ORDER BY *`。
- 输出：反斜杠/Tab/换行转义精确文本、向量紧凑 JSON 精确文本、`SELECT *` 列序。
- 有限内存排序：4 KiB 预算外部归并全量正确、TopK 剪枝、DESC 并列主键升序、临时文件清理、K>max_keep 回退、65536/65537 维边界、启动参数校验。

### 4.5 p3_random_model.py — 模型驱动随机差分（7 种子 × 220 操作，902 次验证）

- 每种子随机序列：多行插入（含故意重复主键/非法向量）、标量/向量/相同值 UPDATE、DELETE、事务、辅助表隔离、EOF 重启、COMMIT 确认后立即 SIGKILL；逐步对照独立内存模型（acked 持久层 + session 事务覆盖层；失败语句整条无效）。
- 边界浮点值：次正规（±1e-45）、相邻 float32、-0.0、float32 最大量级、全零并列。
- 行数越过磁盘哈希初始容量 1024 触发扩容；每次重启后全量状态 + TopK 双重核对。
- 运行：`python -X utf8 tests_cross/p3_random_model.py [seeds...]`（默认 301–303）。

### 4.6 p4_edges.py — 主键边界、码点、自然 checkpoint（103 项，host + 容器各一遍）

- 负数/零/BIGINT 两端主键全链路：数值排序、点查范围、**负主键 TopK 并列升序**、更新删除重启。
- VARCHAR 码点：组合字符（`e\u0301`）、ZWJ 家庭表情（5 码点）、满长 16 码点往返。
- K 恰好 max_keep 边界（8191/8192/9000）对照真值；`--cache-memory-mib 0` 与最小排序预算差分；空表建索引 + TopK。
- 自然触发 checkpoint（低阈值 + 更新制造 WAL 死区）：两次压缩、删除、重启、`checkpoint.wal` 清理。
- 上下文关键字表名（`order` 与 ORDER BY 共存）、大小写折叠重复列、跨行向量字面量。
- `--mode ann` 启动参数与控制行切换；三表不同维度索引交错 + 重启。

### 4.7 p5_benchmark_shape.py — 官方 benchmark 形状检索正确性（19 项，host + 容器各一遍）

依据赛题 7.1 负载形状与 3 VEC-09/召回规则：

- 5,000 行 × 128 维、每批 100 条提交（官方写入形状）、K=10、300 次查询（一半带普通列过滤）。
- 逐条校验 VEC-09：ID 必须存在、无重复、满足过滤条件、距离与当前记录独立真值一致（1e-5）、顺序正确。
- **Recall@10 按官方公式分组计算**：真值在过滤后集合内取 `(distance, primary_key)` 前 K；分母 `min(K, N)`；空集合不参与平均；过滤组与无过滤组**分别**要求 ≥ 0.95（精确路径实测均为 1.0）。
- exact 与 `--mode ann` 双模式各跑一遍（ann 当前为精确回退，验证的是契约而非近似效果）。
- VEC-07 索引维护交错：建索引后 10 增、5 向量更新、5 删除，再查 50 条对照更新后真值。
- TX-07：五个 tag 分组分别比较普通查询集合与 TopK（K≥N）集合，必须完全相等。
- 4.1 派生索引可重建：强杀留下 `flat-*` 文件 → 删除 → 重启，查询仍与真值一致且文件重新生成。

### 4.8 p6_transaction_matrix.py — 事务矩阵与协议契约（42 项，host + 容器各一遍）

逐条对应赛题 4.2/4.1/5.2：

- **TX-01** 自动提交插入/更新/删除，强杀重启后全部保留。
- **TX-02** 事务内普通查询与向量检索均读到自身修改；ROLLBACK 后两者同时恢复（含距离并列按主键升序）。
- **TX-03** 多行插入中途重复主键 → 整条无部分修改，同一语句可重试成功。
- **TX-04** 事务内先成功修改、再失败语句，**COMMIT 与 ROLLBACK 两分支**分别验证；重启后状态正确。
- **TX-05** 事务内向量更新+删除+插入后 TopK 反映变化；ROLLBACK 后 TopK 与基线逐项一致（ID 与距离）。
- **TX-06** COMMIT 确认后立即强杀，重启保留。
- **TX-07** 六个阶段（初始/插入/过滤列更新/向量更新/删除/回滚）普通查询集合与 TopK 集合一致。
- **协议 5.2**：50 条语句同一 PID 连续处理；stdout 仅协议行（READY/OK/RESULT/数据行/END/ERROR）；stderr 为空；单条响应 < 2s；错误后继续可用。
- **READY 时序 4.1**：脏快照 + `during_vector_rebuild` failpoint → 进程终止且**从未输出 READY**，证明就绪在重建完成之后；重启后查询正确。
- **派生索引重建 4.1**：强杀保留 `flat-*` → 删除 → 重启，TopK 与基线一致。
- **数据目录收敛**：无 `sort-*.tmp`/`modify-*.tmp`/`transaction.*`/`checkpoint.wal` 残留；权威 WAL 与派生索引均在数据目录内。

### 4.9 驱动误判审计（内联，8 项注入检查）

对 `tests/test_public_cases.py` 的断言路径注入错误响应：成功不得充当预期错误、错误码精确匹配、未映射语义拒绝、unordered 多重集合不丢重复行、列名/行内容/1e-5 容差均会正确失败。另对 20 组逐组审计 `{}`/`error`/`unordered`/浮点断言分布与 README 声明一致。

## 5. 一键运行清单

```bash
# 宿主机（Python 3.11+；有 g++ 时先构建原生内核）
python -m minivectordb.build_native
python -m unittest discover -v                    # 106 项
python -m tests.run_public_cases --output .local-public.json   # 官方 40 项
python -X utf8 tests_cross/p0_transaction_kill.py              # 19
python -X utf8 tests_cross/p0_vector_modes.py                  # 180
python -X utf8 tests_cross/p1_recovery_timing.py               # 22
python -X utf8 tests_cross/p2_sql_surface.py                   # 166
python -X utf8 tests_cross/p3_random_model.py 301 302 303 304 305 306 307
python -X utf8 tests_cross/p4_edges.py                         # 103
python -X utf8 tests_cross/p5_benchmark_shape.py               # 19（5,000×128 维）
python -X utf8 tests_cross/p6_transaction_matrix.py            # 42

# Linux 容器（镜像内实现；tests/tests_cross/mysqltest 只读挂载）
docker run --rm --network none --cpus=2 --memory=256m --memory-swap=256m \
  -e TMPDIR=/data -v "$PWD/tests:/app/tests:ro" -v "$PWD/mysqltest:/app/mysqltest:ro" \
  --entrypoint python minivectordb:cross-review -m unittest discover -v
docker run --rm --network none --cpus=2 --memory=256m --memory-swap=256m \
  -v "$PWD/tests_cross:/app/tests_cross:ro" \
  --entrypoint python minivectordb:cross-review /app/tests_cross/p2_sql_surface.py
# p4_edges.py 同法；p0/p1/p3 依赖宿主机进程模型与 failpoint，在容器内以外路径另行覆盖

# 真实入口生命周期与超内存压力
python -m tests.docker_validation --image minivectordb:cross-review --output .local-lifecycle.json
docker run --rm --network none --cpus=2 --memory=64m --memory-swap=64m \
  -e TMPDIR=/data -v "$PWD/tests:/app/tests:ro" \
  --entrypoint python minivectordb:cross-review -m tests.container_vector_pressure
```

## 6. SQL 语句规模（实测）

以下为 2026-11-10 用桩计数实测的 SQL 语句数（每条发送给数据库的语句计 1，含建表/插入/查询/事务控制；驱动指令与模式控制行不计）：

| 层 | 用例/检查数 | SQL 语句数 |
| --- | ---: | ---: |
| 官方公开题包（typed 路径一遍） | 20 组 / 40 项检查（双路径） | **95** |
| 仓库自测套件（106 项） | 106 | **3,807** |
| p0_transaction_kill | 19 | 51 |
| p0_vector_modes | 180 | 783 |
| p1_recovery_timing | 22 | 42 |
| p2_sql_surface | 166 | 162 |
| p3_random_model（7 种子） | 902 次验证 | 1,619 |
| p4_edges | 103 | 101 |
| p5_benchmark_shape | 19 | 753 |
| p6_transaction_matrix | 42 | 162 |
| **独立组小计** | **1,453** | **3,673** |
| **合计（不含双路径重复）** | — | **约 7,575** |

说明：官方 95 条为权威计数（manifest 声明并经脚本逐文件核对）；套件与独立组为实测值，因随机种子与循环生成而每次运行略有差异。p0/p1 的语句数较少是因为其检查以"重启后核对"为主，一条语句配多次状态验证。

## 6. 覆盖边界（本总览不作结论的范围）

- 正式协议（stdin/stdout 格式、错误码映射、资源限额、隐藏用例输入）未公布，全部本地结论基于公开题包与本地临时协议，不能推导正式准入。
- p0/p1/p3 的强杀与 failpoint 时序在宿主机 Windows 进程模型下验证（`Process.kill()`）；容器内真实 SIGKILL 由 `docker_validation` 与公开题包重启路径覆盖。
- 未测试断电、介质损坏、外部篡改、多进程并发（题目不要求）；ANN 当前为精确扫描回退，无近似索引可测召回。
- 历史 `v4-*`、r2/r3 报告绑定旧版本摘要；以各报告标注的版本为准，不得互相重标。
