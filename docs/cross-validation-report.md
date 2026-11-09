# MiniVectorDB 独立交叉验证报告

日期：2026-11-09 至 2026-11-10。执行者：未参与开发的独立 Agent，按 `docs/agent-cross-validation.md` 交接执行。约束遵守情况：未推送、未消耗正式提测机会、未修改 `mysqltest/` 原始题包、未改写 `agent/conversation.json`、未覆盖任何 `v4-*` 历史报告。

## 1. 验证对象

| 项目 | 值 |
| --- | --- |
| 仓库 | `E:\MINIOB\mini-ob\minivectordb-round2` |
| 分支 | `2026-competition-round2` |
| HEAD | `965c59d70ec53d3ec85f899cd64f012ad7074c4f`（与交接一致） |
| 已合入官方题包 | `9f6df2ba`（minivectordb-public-v4，20 组 95 条 SQL） |
| 工作区差异（验证前已存在） | `agent/conversation.json`（修改，未提交学习记录）；`docs/agent-cross-validation.md`（新增） |
| 本次评审新增（均未跟踪） | `tests_cross/`（4 个独立测试脚本 + 1 个进程清理脚本）、`docs/cross-validation-report.md`（本文件）、`.local-cross-*` 原始日志 |
| 宿主机 | Windows 11 x64（10.0.26200），Git Bash；Python 3.11.9；MinGW g++ 8.1.0 |
| 容器 | Docker 28.4.0（linux/amd64），镜像 `minivectordb:cross-review` = `sha256:241d2b2b4d552f2b4a326b2bc11fda97efd2e101f61825ecfffda3b3cfd9d875` |
| 容器资源参数 | 功能测试 2 CPU / 256 MiB / 无 swap / 断网 / `TMPDIR=/data`；生命周期与压力测试 64 MiB |
| 上游核对 | `git ls-remote origin`：官方 `2026-competition-round2` tip 仍为 `9f6df2ba`，验证期间无新题包 |

实现源码在验证期间**零修改**。原生内核宿主机重建（`minivectordb/_native.dll`，g++ 8.1.0）。

## 2. 执行清单

### 2.1 复现原开发验证（全部通过）

| 命令 | 环境 | 结果 | 原始日志 |
| --- | --- | --- | --- |
| `python -m tests.validate_submission` | 宿主机 | 通过；`git_blob_verified:false` 符合预期（工作区含未提交材料） | stdout 记录于会话 |
| `python -m tests.run_public_cases --output .local-cross-public-host.json` | 宿主机 | 40/40 通过（typed+stdio，20 组） | 同上 |
| `python -m unittest tests.test_public_adapter -v` | 宿主机 | 4/4 通过 | 同上 |
| `python -m minivectordb.build_native` | 宿主机 | 构建成功 | 同上 |
| `python -m unittest discover -v` | 宿主机 | **106/106** 通过，32.5s，无跳过 | `.local-cross-host-full.txt` |
| `docker build --platform linux/amd64 -t minivectordb:cross-review .` | Docker | 成功，镜像 ID 见上 | `.local-cross-docker-build.log` |
| `docker run … -m unittest discover -v` | 容器 256MiB | **106/106** 通过，21.8s | `<out>/linux-full-tests.txt` |
| `docker run … -m tests.run_public_cases --output /results/public.json` | 容器 256MiB | **40/40** 通过 | `<out>/public.json` |
| `python -m tests.docker_validation --image minivectordb:cross-review …` | 宿主机+容器 64MiB | 4/4 检查通过（SIGKILL 保留已确认提交、EOF 回滚、PID1 SIGTERM 回滚、同卷重启），无 OOM | `<out>/lifecycle.json` |
| `docker run … -m tests.container_vector_pressure` | 容器 64MiB | 通过：18,000×1024 维（向量文件 73,728,000 字节 > 内存限额），`memory_peak=67,108,864`、`oom_kill=0`、65.8s | `<out>/vector-pressure.txt` |

容器内挂载仅 `tests/`、`mysqltest/`、`native/`（供哈希读取）与结果目录；未挂载宿主机 `minivectordb/` 覆盖镜像实现。与历史 v4 结果（106 项/40 项）一致。

### 2.2 独立新增测试（全部通过）

| 脚本 | 检查数 | 环境 | 内容 | 原始日志 |
| --- | --- | --- | --- | --- |
| `tests_cross/p0_transaction_kill.py` | 19 | 宿主机 | 失败语句原子性、事务四分支、**COMMIT 响应后 4.6ms 立即 SIGKILL**（交接文档指出的专门时序缺口）、自动提交失败、维度错误、多行 UPDATE 中途失败 | `<out>/p0-transaction-kill.txt` |
| `tests_cross/p0_vector_modes.py` | 180 | 宿主机 | 事务内 `--mode ann/exact` 切换（进程未替换）、过滤缓存刷新（改过滤列/零行/相同值/失败语句）、事务内交错增删改 × COMMIT/ROLLBACK 双分支、300 行随机数据 native 与 `--disable-native` 对照**独立真值**（`struct.pack('f')` + `math.dist` + `(distance,pk)` 排序）、N=0/N<K/K=0/LIMIT 边界、等距并列、float32 语义与极值、近似/精确并列、非法向量、DDL 回滚等本地约定 | `<out>/p0-vector-modes.txt` |
| `tests_cross/p1_recovery_timing.py` | 22 | 宿主机 | 恢复时序矩阵：T1 提交前强杀；T2/T2b 提交请求已发出无响应（WAL 已同步→完整生效；footer 前→完整未生效）；T3 自动提交确认后强杀；T4 恢复途中连续两次强杀后第三次启动完整恢复；T5 脏向量快照重建途中强杀；T6 checkpoint 压缩发布前/后强杀（已提交删除保留、残留文件清理） | `<out>/p1-recovery-timing.txt` |
| `tests_cross/p2_sql_surface.py` | 166 | 宿主机 + **容器各一遍** | SQL 表面（注释/转义引号/串内分号/多行/反引号/大小写/`BEGIN TRANSACTION`/`COMMIT WORK`）、BIGINT 2^63 边界、VARCHAR 按 Unicode 字符计长、码点比较排序、非法向量全谱（NaN/Infinity/溢出/维度/空/布尔/嵌套/JSON 错/引号嵌套）、不支持语法必须报错（OR/IN/NOT/聚合/JOIN/NULL/主键更新/重复赋值/别名规则）、输出转义精确文本、向量紧凑 JSON、4KiB 排序预算外部归并与 TopK 剪枝、临时文件清理、K>max_keep 回退、65536/65537 维边界、启动参数校验、模式控制行作用域 | `<out>/p2-sql-surface.txt`、`<out>/p2-sql-surface-linux.txt` |
| 驱动误判注入（内联） | 8 | 宿主机 | 对 `tests/test_public_cases.py` 注入错误响应：成功不得充当预期错误、错误码精确匹配、未映射语义拒绝、unordered 多重集合不丢重复行、列名/行内容/1e-5 浮点容差均会失败 | 会话记录 |
| 题包断言审计（内联） | 20 组 | 宿主机 | 逐组统计 `{}`/`error`/`unordered`/浮点断言分布，与 README 声明一致 | 会话记录 |

**独立测试合计 401 项检查，0 失败。** 所有独立脚本不依赖被测仓库的测试工具（自带管道客户端），真值独立建立。

## 3. 发现

### 3.1 已证实的功能缺陷

**无。** 在上述 401 项独立检查、549 项复现检查中，未发现事务、恢复、SQL 语义、向量检索、过滤缓存或驱动断言层面的功能缺陷。所有测试期问题经逐条排查均为本次评审自有脚本的预期错误（详见 §3.4）。

### 3.2 观察与建议（非缺陷，按严重度排序）

| 编号 | 严重度 | 观察 | 依据与建议 |
| --- | --- | --- | --- |
| O-1 | P2 | `ORDER BY l2_distance`（使用未加别名的隐含列标签）报 `NOT_FOUND`；官方公开用例均使用 `AS distance` 别名，故未触发 | 隐藏用例若引用隐含标签会失败。建议：在 `engine.select` 的排序绑定中把未别名距离表达式的标签纳入 `aliases`（约 `minivectordb/engine.py:290`）。未实施，仅建议；是否适配取决于正式题包语法范围 |
| O-2 | P2 | 相同值 `UPDATE` 的 `affected_rows` 按命中行计（非 MySQL 的 changed-rows 语义） | 本地约定；公开题包未覆盖此语义。已在 `docs/` 各处标注为待题包核对的 OPEN 项，保持现状即可 |
| O-3 | P3 | 非法启动参数（如 `--sort-memory-mib 0.001`）使 `Engine()` 构造的 `DBError` 未被 `__main__.main` 捕获，进程以 traceback + exit 1 退出，stdout 无协议输出 | 启动失败而非语句路径，行为可接受。若希望统一，可在 `__main__.py` 构造段补 `except DBError`。未实施 |
| O-4 | P3 | Windows 文本管道下裸 `\r` 到达数据库前被 universal-newline 翻译为 `\n`；Linux 容器内 `\r` 原样保留且转义输出正确（`a\rb`） | 无缺陷；仅记录双平台差异，供协议调试时区分传输层与实现层 |
| O-5 | 信息 | `ann` 模式当前复用精确扫描（README 已声明）；两种模式结果相同 | 不影响功能准入（精确结果满足召回）；影响性能榜。属已知交付边界 |
| O-6 | 信息 | 纯注释行不产生任何协议响应（缓冲直到出现真实 SQL） | 本地约定行为，已记录 |
| O-7 | 环境 | Git Bash 下 `docker --mount type=bind,source=…` 的 POSIX 路径改写导致容器内挂载点错位；改用 `-v` + `MSYS_NO_PATHCONV=1` 后正常 | 工具链问题，非项目缺陷；写入本报告供后续在 Git Bash 环境复现时避坑 |

### 3.4 评审脚本自身的错误（透明记录）

首轮运行中 9 处失败经逐条复现确认均为本次评审脚本预期错误，已修正后重跑全绿：行内注释吞分号导致语句不终止（`;` 须在注释前）；码点序中 `a`(U+0061) 小于汉字被误排；SELECT 误用 OK 断言；索引名冲突用例误用标量列；`L2_DISTANCE` 用于 WHERE 的报错类别预期（词法/操作符位置即拒绝）；真值元组与结果行错位 zip（P0 第二组首轮）；场景 G 建表维度与插入不符。这些问题**均非被测实现缺陷**，修正过程未改动任何实现源码。

## 4. 修复建议汇总

无必须修复项。可选优化见 §3.2 O-1/O-3（均未实施，不影响当前功能正确性结论）。若采纳 O-1，应补回归：未别名 `ORDER BY l2_distance` 与别名版本结果一致，且不破坏现有别名优先级。

## 5. 覆盖边界

以下内容**未测试或无法测试**，报告不对其作结论：

1. **正式协议**：官方 stdin/stdout 格式、语句边界、错误码映射、就绪标识、资源限额仍未公布（上游验证期间无新提交）。本报告全部结论基于本地临时协议（README 声明），不能推导正式准入。
2. **上次正式提测失败**：用户反馈的"MySQL 未通过"无详细日志，本次验证未能定位其根因；本地全绿与该反馈并存，失败最可能位于本地不可复现的环节（协议适配、隐藏用例输入、资源配置）。
3. **崩溃模型**：宿主机 `Process.kill()` 在 Windows 上是 `TerminateProcess`（非 POSIX SIGKILL）；容器内真实 SIGKILL 由 `docker_validation`（`docker kill --signal KILL`）覆盖，其 COMMIT 与强杀之间有后续 SQL；"COMMIT 响应后立即杀"的专门时序仅在宿主机独立验证（`p0_transaction_kill.py`）。未覆盖断电、磁盘损坏、外部篡改（题目不要求）。
4. **恢复时序粒度**：恢复途中再杀使用 `during_recovery`/`during_vector_rebuild` failpoint（每条记录应用处触发）加连续两次强杀；未穷举恢复循环的每条指令边界。
5. **数据规模**：压力测试为 18,000×1024 维/64MiB；正式 benchmark 规模（10,000×128 维、三轮、Recall@10≥0.95）与冻结资源限额未知，未测量性能榜指标。
6. **并发**：单进程单客户端为题目范围；多进程同目录并发未测试（README 声明不支持）。
7. **学习材料**：`agent/conversation.json`（127,442 字节）仅经 `validate_submission` 做 JSON/大小/密钥校验通过；内容评审不在本次范围。交叉验证结果未写入该文件。

## 6. 结论

在 HEAD `965c59d`、验证期间无官方更新的前提下：公开 v4 题包（typed+stdio 双路径）、Linux 镜像内完整回归、真实入口生命周期、64MiB 内存压力全部复现通过；在此基础上以 401 项独立反例覆盖失败语句原子性、COMMIT 确认时序、事务中模式切换、过滤/索引缓存一致性、独立真值 TopK、恢复时序矩阵、驱动误判与 SQL/类型边界，**未发现功能缺陷**。剩余风险集中于尚未公布的正式协议与隐藏用例（§5.1、§5.2），该风险不能通过本地测试消除。
