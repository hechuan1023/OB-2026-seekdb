# MiniVectorDB：独立 Agent 交叉验证交接

本文件供未参与开发的 Agent 直接上手。任务是独立寻找错误、核对证据和测试边界，不是替现有实现证明正确。请先完成验证并报告问题；修复建议与已验证结论分开。未经用户另行要求，不推送、不触发正式评测、不改写学习材料。

交接日期：2026-11-09。下文版本与结果是交接时快照，不表示之后的官方仓库或本地工作区没有变化。

## 1. 找到正确项目并锁定验证对象

| 项目 | 值 |
| --- | --- |
| 本机项目根目录 | `E:\MINIOB\mini-ob\minivectordb-round2` |
| 参赛仓库 | https://github.com/hechuan1023/OB-2026-seekdb |
| 开发分支 | `2026-competition-round2` |
| 交接时 HEAD | `965c59d70ec53d3ec85f899cd64f012ad7074c4f` |
| 已合入官方题包提交 | `9f6df2ba5acf871376e22b64a3d0724e4b575044` |
| 官方来源 | https://github.com/oceanbase/seekdb |
| 实现目录 | `minivectordb/`、`native/exact.cpp` |
| 公开题包 | `mysqltest/`，版本 `minivectordb-public-v4` |
| 学习材料 | `agent/conversation.json` |

这是 seekdb 仓库中的自研 MiniVectorDB 项目；不要误入父目录其他项目，也不要改为编译上游 OceanBase 服务。

交接时，`agent/conversation.json` 有尚未提交的真实学习记录；数据库源码无未提交改动。本交接文件也是新文件。开始前用 `git status --short` 核对，不清理或覆盖这些修改。其他机器克隆上述提交只能取得已提交材料，不能假定包含本机的新学习记录。

PowerShell 起步：

```powershell
Set-Location E:\MINIOB\mini-ob\minivectordb-round2
git status --short
git branch --show-current
git rev-parse HEAD
python --version
docker version
```

新机器如需克隆，使用不含凭证的 URL，并保留完整历史（材料校验会读取历史提交）：

```text
git clone --branch 2026-competition-round2 https://github.com/hechuan1023/OB-2026-seekdb.git minivectordb-review
```

访问私有仓库时使用已配置的认证，不把 token 写入命令、报告或对话。若要验证上述固定 SHA，在独立 checkout/worktree 中操作；不要在用户有修改的工作区强行切换版本。

## 2. 先读哪些文件

所有下列相对路径均以项目根目录为基准。

1. `AGENTS.md`：本仓库操作约束。
2. `docs/minivectordb-requirements.md`：赛题要求，尤其 SQL、事务、恢复、资源约束。
3. `README.md`：实现范围、入口、本地协议、限制。
4. `mysqltest/README.md` 与 `mysqltest/manifest.json`：公开断言和驱动指令；以原始文件为准。
5. `docs/public-v4-validation.md`：当前版本已做验证与边界。
6. `docs/design.md`、`docs/validation.md`：设计与历史实验。注意历史结果的版本，不能全部当成当前版本重测结果。

**已知但尚未解决的事实：**用户曾反馈正式 MySQL 准入未通过，只有汇总，没有详细错误或可确认的失败版本。交接所依据的公开 v4 包不包含完整官方运行驱动；正式 stdin/stdout 格式、冻结错误码和资源配置仍有缺口。不要把推测写成失败根因，也不要用本地通过抹去正式失败反馈。若发现更新题包，先报告新旧差异及来源，再决定适配范围。

## 3. 复现已有验证

### 3.1 宿主机快速检查

项目需要 Python 3.11+，Python 部分只使用标准库；原生内核需要支持 C++17 的 g++。以下命令在项目根目录运行；每条结束后检查退出码，非零时保留输出，不直接忽略后续继续。

```powershell
python -X utf8 -m tests.validate_submission
python -X utf8 -m tests.run_public_cases --output .local-cross-public-host.json
python -X utf8 -m unittest tests.test_public_adapter -v
```

有本机编译器时，先重新构建，再执行完整套件：

```powershell
python -X utf8 -m minivectordb.build_native
python -X utf8 -m unittest discover -v
```

没有 g++ 时使用下面的 Docker 构建验证原生路径。记录测试的 skip；跳过原生测试不能报告为原生功能已通过。宿主机 Python 路径可作为对照，不能代替 Linux 容器和 cgroup 资源验证。

`validate_submission` 校验 JSON、源码摘要和历史报告关联，不执行正式协议验证。当前工作区学习材料未提交，`--git` 会额外检查 HEAD 与工作区字节相同，因此不应期待它此刻通过。若修改源码，摘要不匹配应如实报告，不能为了让校验变绿就把旧测试结果重新标成新版本结果。

### 3.2 从源码构建独立镜像

以下为 Windows PowerShell 命令，使用单行避免 Bash 续行符混用。Docker 应处于 Linux containers 模式；运行目标为 linux/amd64。

```powershell
docker build --platform linux/amd64 -t minivectordb:cross-review .
docker image inspect minivectordb:cross-review --format '{{.Id}} {{.Os}}/{{.Architecture}}'
$reviewRoot = (Get-Location).Path
$reviewOut = Join-Path $reviewRoot ('.local-cross-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $reviewOut | Out-Null
```

Dockerfile 已锁定基础镜像摘要和构建包版本。构建期间可联网，数据库运行期间断网。如果构建因镜像、网络、依赖版本不可获取而失败，记录为构建/环境阻塞，不能默默换依赖或复用旧镜像后宣称构建成功。

### 3.3 镜像内完整测试与公开题包

```powershell
docker run --rm --network none --cpus=2 --memory=256m --memory-swap=256m -e TMPDIR=/data --mount "type=bind,source=$reviewRoot/tests,target=/app/tests,readonly" --mount "type=bind,source=$reviewRoot/mysqltest,target=/app/mysqltest,readonly" --entrypoint python minivectordb:cross-review -m unittest discover -v
docker run --rm --network none --cpus=2 --memory=256m --memory-swap=256m -e TMPDIR=/data --mount "type=bind,source=$reviewRoot/tests,target=/app/tests,readonly" --mount "type=bind,source=$reviewRoot/mysqltest,target=/app/mysqltest,readonly" --mount "type=bind,source=$reviewRoot/native,target=/app/native,readonly" --mount "type=bind,source=$reviewOut,target=/results" --entrypoint python minivectordb:cross-review -m tests.run_public_cases --output /results/public.json
```

挂载的只有测试、公开题包、供报告哈希读取的 C++ 源文件和结果目录；**不要挂载宿主机 `minivectordb/` 覆盖镜像实现**。`/results` 只用于测试驱动输出报告；数据库数据和临时文件仍在 `/data`。这里的 `--entrypoint python` 用于运行测试套件，真实入口另由下一步验证。Linux/macOS 可把 `$reviewRoot` 换为项目绝对路径，保留同样的挂载边界。

### 3.4 真实入口、强杀与资源压力

```powershell
python -X utf8 -m tests.docker_validation --image minivectordb:cross-review --output "$reviewOut/lifecycle.json"
docker run --rm --network none --cpus=2 --memory=64m --memory-swap=64m -e TMPDIR=/data --mount "type=bind,source=$reviewRoot/tests,target=/app/tests,readonly" --entrypoint python minivectordb:cross-review -m tests.container_vector_pressure
```

生命周期驱动会创建唯一命名的容器和卷，并清理它自己创建的资源；不需要操作用户原有数据卷。压力脚本假定全新 `/data`，涉及 18,000 条 1,024 维向量，可能耗时较长，应单独执行并保留完整输出。64/256 MiB 是本地验证配置，不是已经确认的正式限额。

现有生命周期脚本检查了已确认提交、后续未提交修改与脏向量快照一起经历强杀的恢复行为；它在 COMMIT 与强杀之间还执行了其他 SQL，**不能将其描述为“收到 COMMIT 响应后立即强杀”的专门测试**。请独立补这一时序。

### 3.5 历史预期，仅用于对照

| 记录 | 已保存结果 |
| --- | --- |
| `docs/public-v4-retest.json` | 20 组公开用例 × typed/stdio 两条路径，共 40 项通过 |
| 每条公开执行路径 | 95 条 SQL、7 次正常重启、1 次模式切换、4 个预期错误 |
| `docs/v4-linux-full-tests.txt` | 106 项通过，22.885 秒 |
| `docs/v4-windows-tests.txt` | 106 项通过，41.958 秒 |
| `docs/v4-lifecycle.json` | 实际镜像强杀、EOF、SIGTERM 与同卷恢复通过 |

这些是原开发 Agent 的既有结果，不是你的交叉验证结果。按自己的版本、机器、命令重新记录结果；耗时无需与历史相等。若新增测试，测试总数自然可能变化。

## 4. 自己写驱动时的协议

本节仅描述当前**本地临时协议**，不冒充官方协议：

- 前台长驻进程，stdin 收 UTF-8 SQL，每条以分号结束；stdout 及时刷新；日志走 stderr。不要使用 TCP/MySQL 客户端。
- 启动恢复完成：`READY`。
- 成功：`OK<TAB>受影响行数`。
- 查询：`RESULT<TAB>列名...`，随后 TSV 行，最后 `END<TAB>行数`。
- 错误：`ERROR<TAB>错误类别<TAB>说明`。错误后继续读取下一语句。
- 输出字符串转义反斜杠、Tab、换行和回车；解析时需还原，不能直接用行文本替代所有类型检查。
- EOF、SIGTERM 正常退出；未提交事务回滚。SIGKILL 不走正常关闭流程。
- 启动参数 `--mode exact|ann`；默认 exact。`--disable-native` 强制 Python 路径。
- 显式启用 `--local-mode-controls` 后，完整 SQL 之间的独立行 `--mode ann` 或 `--mode exact` 才是本地模式控制，响应 `OK<TAB>0`。不要给该控制行加 SQL 分号。
- 题包 `--restart` 由驱动关闭进程、等待退出、复用同一数据目录再启动，不能当 SQL 发给数据库。
- 模式切换必须保留当前进程和事务；不能用重启代替，也不能擅自 COMMIT。

直接交互入口：

```powershell
python -X utf8 -m minivectordb --data-dir .local-cross-manual --local-mode-controls
```

每个独立场景使用不同的新目录。强杀验证应使用管道驱动：等待明确成功响应后再 kill，并发读取 stderr 防止管道阻塞，给响应和退出设置超时。不要先关闭 stdin 再 kill，否则可能已正常关闭。容器场景应杀容器内数据库，例如 `docker kill --signal KILL <本次容器名>`，不能只杀宿主机 docker 客户端。

## 5. 独立验证重点与预期

先根据题目建立独立预期，再阅读实现解释。仅重跑现有测试不算充分的交叉验证；但不要把同一缺陷换参数重复计成多个发现。

### P0：失败语句与事务边界

每个分支从独立空目录开始，执行：

```sql
CREATE TABLE t (id BIGINT PRIMARY KEY, content VARCHAR(16));
INSERT INTO t VALUES (1, '原值'), (2, '保留');
BEGIN;
UPDATE t SET content = '已修改' WHERE id = 1;
INSERT INTO t VALUES (3, '新增'), (2, '重复');
SELECT id, content FROM t ORDER BY id;
```

最后一条 INSERT 必须报重复主键错误；SELECT 只能返回 `(1, 已修改)`、`(2, 保留)`，无 id=3。

| 后续分支 | 重启后的预期 |
| --- | --- |
| COMMIT 成功，再正常退出重启 | id=1 已修改；id=2 保留；无 id=3 |
| ROLLBACK 成功，再正常退出重启 | id=1 原值；id=2 保留；无 id=3 |
| COMMIT 响应后立即 SIGKILL，再重启 | 与已提交分支相同 |
| 不发 COMMIT，直接 SIGKILL，再重启 | 与原始已提交数据相同 |

迁移反例：去掉 BEGIN 测自动提交失败；把后一行冲突改为向量维度错误或 VARCHAR 超长；让 UPDATE 命中多行并在后续行才失败。分别检查先前成功语句保留、失败语句无残留、后续仍能提交或回滚。先确认非法输入是题目要求拒绝的，不自行扩展 SQL 标准。

### P0：模式切换不能丢事务

用 `--local-mode-controls` 启动，从空目录执行以下 SQL 和控制行：

```text
CREATE TABLE v (id BIGINT PRIMARY KEY, embedding VECTOR(2));
INSERT INTO v VALUES (1, '[9,9]'), (2, '[1,0]');
CREATE VECTOR INDEX vi ON v(embedding);
BEGIN;
UPDATE v SET embedding = '[0,0]' WHERE id = 1;
--mode ann
SELECT id FROM v ORDER BY L2_DISTANCE(embedding, '[0,0]') LIMIT 1;
ROLLBACK;
--mode exact
SELECT id FROM v ORDER BY L2_DISTANCE(embedding, '[0,0]') LIMIT 1;
```

第一次查询必须为 id=1，回滚后为 id=2；驱动还应确认进程未被替换。当前 ann 复用精确扫描，不代表实现了近似索引；这不豁免模式、过滤和事务语义。

### P0：过滤、索引缓存和独立真值

- 构造全表最近记录都不满足 WHERE、但过滤后还有足够记录的场景，排除“全表取 K 再过滤”。
- 建索引后交错 INSERT、DELETE、向量 UPDATE、普通过滤列 UPDATE；在事务内、COMMIT 后、ROLLBACK 后分别比较普通查询与 TopK。
- 先查询建立过滤缓存，再改过滤列而不改向量，确认候选集合刷新。零行修改、相同值修改、失败语句也要保证结果正确。
- 覆盖 N=0、N<K、K=0、等距主键排序、LIMIT 边界；普通 ORDER BY 并列项同样按主键升序。
- 独立真值先将向量每个元素舍入到 float32，例如标准库 `struct.pack/unpack('f', value)`，再以 Python 双精度 `math.dist` 求欧氏距离，按 `(distance, primary_key)` 排序。不要复用被测代码的距离、过滤或排序函数。
- 用固定随机种子对照原生与 `--disable-native` 路径，但两条实现彼此一致不自动等于正确，仍需独立真值。参考测试模型可以仅针对小数据使用内存字典，不要求它本身成为数据库。
- float32 极值、近似等距、非法向量必须单独验证。公开浮点容差为绝对/相对 `1e-5`；不要用宽容差掩盖错误的主键或排序。

### P1：恢复与有限内存

- COMMIT 请求前、请求发出但无成功响应、已读到成功响应三类时点，按题目分别检查：未提交回滚；允许完整提交或完整未提交；必须保留提交。任何情况都不能部分生效。
- 在恢复途中再次终止，随后第三次启动核对；记录确实在 READY 前终止的证据。一个偶然时序通过不能代表覆盖所有崩溃窗口。
- 审核 WAL 追加、同步、成功响应顺序，以及日志压缩发布与索引重建。只读代码推断和实际故障实验要分开报告。
- 数据超过容器内存预算时，检查插入、排序、查询、恢复；同时观察 cgroup 内存和 OOM。RSS、tracemalloc、文件大小分别不能替代 memory.peak、容器峰值、物理 I/O。
- 小数据生命周期测试通过，不能证明大数据恢复耗时和资源约束满足正式限制。当前正式冻结限额缺失时明确保留结论边界。

### P1：检查测试驱动本身是否会误判通过

- `{}` 表示该 SQL 必须成功，不是忽略结果和错误。
- 预期错误时必须验证语义，不能“任何错误都算通过”，更不能把成功算通过。
- `--restart`、`--mode` 不占 SQL 断言位置；每组初始 exact，未知驱动指令应显式失败。
- `unordered` 应按多重集合比较，不能用 set 丢掉重复行；列名和顺序必须检查。
- 对驱动注入错误响应，确认会失败。可参考 `tests/test_public_adapter.py`，但需要独立判断其覆盖是否足够。
- 原始 `mysqltest/` 不得修改来迁就实现；新反例放在独立测试文件。

## 6. 源码导航

| 文件 | 核查重点 |
| --- | --- |
| `minivectordb/sql.py` | 分词、解析、语句边界、错误输入 |
| `minivectordb/engine.py` | 语句恢复点、事务可见性、修改与查询执行 |
| `minivectordb/storage.py` | WAL、事务暂存、磁盘主键索引、恢复、压缩 |
| `minivectordb/vector_index.py` | 向量快照、过滤缓存、失效、回退 |
| `native/exact.cpp` | 精确 TopK、浮点计算、候选与并列边界 |
| `minivectordb/sorting.py` | 有限内存排序和归并 |
| `minivectordb/protocol.py`、`__main__.py` | 本地协议、刷新、信号、模式控制 |
| `tests/test_public_cases.py` | 题包适配、typed/stdio 断言 |
| `tests/test_recovery.py`、`docker_validation.py` | 管道客户端、退出与恢复测试 |

## 7. 交付报告的要求

把新结果保存在独立文件，例如 `docs/cross-validation-report.md`；原始日志先放唯一的 `.local-cross-*` 目录。不要覆盖 `v4-*` 历史报告。没有执行就写“未执行”，测试阻塞就写明阻塞点，不补造输出。

报告至少包含：

1. 验证对象：commit、工作区差异、操作系统、Python、镜像 ID、资源参数；修改前后分别标记。
2. 执行清单：实际命令、退出码、通过/失败/跳过数量、对应原始日志；区分原开发测试与独立新增测试。
3. 每个发现：严重度、要求依据、最小可复现输入、预期和实际、稳定复现次数、相关源码位置。环境阻塞、协议待定、已证实功能缺陷分别说明。
4. 修复建议：机制解释、影响范围、应补的回归；没有实施的修复不能写成已修复。
5. 覆盖边界：未测试的时序、数据规模、路径、正式协议缺失；不使用“通过所有隐藏用例”或“可保证正式准入”的结论。

优先级建议：P0 为数据丢失、事务/查询正确性或驱动误判；P1 为资源、边界、恢复可靠性；纯性能优化在正确性排查之后。

学习材料另外处理：评审读取单个 UTF-8 JSON 顶层对象，最多 131,072 字节，不能有重复键、NaN/Infinity 或密钥。交接时本机材料为 127,442 字节，只余 3,630 字节；新增记录前必须重新量字节数。你的测试与解释只能标为 Agent/工具来源，不能替用户写“我理解了”。不要将交叉验证全文直接塞入材料；先向用户交付实际结果，再按真实后续互动决定收录。

## 8. 可直接发给另一个 Agent 的任务

> 请独立交叉验证 `E:\MINIOB\mini-ob\minivectordb-round2` 中的 MiniVectorDB。先读 `AGENTS.md` 和 `docs/agent-cross-validation.md`，确认实际版本与工作区差异。按交接文档复现公开用例、Linux 镜像与恢复测试，再独立设计反例，重点排查失败语句原子性、事务中切换 ann、过滤/索引缓存一致性及 COMMIT 成功后立即强杀。不要只认可现有测试结论，也不要把未知官方协议推测成确定缺陷。保留用户未提交修改和原始题包，不自动推送、不消耗正式提测机会、不编造学生学习记录。输出带最小复现、实际命令结果和覆盖边界的交叉验证报告；先报告发现与修复建议，再由用户决定后续修复范围。
