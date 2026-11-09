# 交叉验证后性能优化记录

2026-11-10。执行者：交叉验证 Agent，在正确性排查收口（见 [交叉验证报告](cross-validation-report.md)）后按用户指示进行。约束：未推送、未消耗正式提测机会、未改 `agent/conversation.json` 与 `mysqltest/`。

## 基线与版本

| 版本 | 载体 | 说明 |
| --- | --- | --- |
| 基线 | HEAD `965c59d` 源码；容器镜像 `minivectordb:public-v4` = `sha256:e125eda8…` | 优化前，与交叉验证对象同源 |
| 优化后 | 工作区修改（`sql.py`、`engine.py`、`native/exact.cpp`）；容器镜像 `minivectordb:cross-review` = `sha256:f1feb8af…` | 本轮产物，**尚未提交** |

两版使用同一测量驱动（宿主机 `tests/benchmark.py` 与挂载进容器的 `tests/container_benchmark.py`）。

## 三项修改（语义不变的依据）

1. **`minivectordb/sql.py`**：字符串/反引号 token 正则改为占有优先量词 `[^']*+(?:''[^']*+)*+`（Python 3.11+）。剖析显示 128 维查询向量（2.5KB 字面量）与 100 行大 INSERT（60KB）在旧 `'(?:''|[^'])*'` 下逐字符双交替回溯。转义语义不变（`''` 仍为引号转义）。
2. **`minivectordb/engine.py` `convert()`**：删除 float64 阶段的 `isfinite` 预遍历，改为 类型检查 → `array('f')`（捕获 float64→f32 溢出，`OverflowError`）→ `all(map(math.isfinite, result))` 单遍。NaN/Infinity/溢出/类型错误仍全部 `INVALID_VECTOR`，仅错误说明文字不同（协议未冻结错误文案）。
3. **`native/exact.cpp`**：单链 Kahan 补偿和（循环依赖，阻止自动向量化）改为 **4 条独立补偿链**：维度 `j` 归 lane `j&3`，各链内部严格串行，合并顺序固定 → 无论编译器选择何种 SIMD 宽度，结果**逐位相同**；Linux/GCC 加 `target_clones("default","avx2")` 运行时分发，`-ffp-contract=off`（既有构建参数）保证各 clone 逐位一致。ABI 保持 1：对外距离值仍由 Python `math.dist` 复核返回，内核距离仅用于候选选择，其边界歧义由既有的 `1e-12` 相对间隙守卫回退到参考执行器（`vector_index.py:170`）。Windows（MinGW 无 ifunc）编译基线 clone，SSE2 自动向量化仍生效。

未改动：WAL 格式与 fsync 时机、快照失效策略（r3 成果保留，快照构建次数与字节完全一致）、事务/恢复语义。

## 测量结果

### 宿主机 benchmark（10,000 行 × 128 维、1,000 查询、种子 20261109）

| 指标 | 基线 | 优化后 | 变化 |
| --- | ---: | ---: | ---: |
| native 写入 | 3.0041s | 1.5285s | **-49.1%** |
| native 查询 | 2.1892s | 1.2692s | **-42.0%** |
| native 恢复 | 0.4907s | 0.5095s | -3.8%（噪声级，WAL 重放未动） |
| Python 路径写入 | 3.5587s | 1.4995s | **-57.9%** |
| Python 路径查询 | 32.1623s | 31.9751s | -0.6%（通用执行器全扫描以 `math.dist` 为主，不在本轮目标内） |

### 容器 benchmark（2 CPU / 128 MiB / 断网，各 **1 轮**，非三轮中位数）

| 指标 | 基线 `e125eda8` | 优化后 `f1feb8af` | 变化 |
| --- | ---: | ---: | ---: |
| 写入 | 3.1282s | 2.1978s | **-29.7%** |
| 静态查询合计（中位单次） | 3.7340s（3.91ms） | 2.7224s（2.67ms） | **-27.1%** |
| 无过滤查询合计 | 2.3706s | 1.5510s | -34.6% |
| 过滤查询合计 | 1.3634s | 1.1714s | -14.1% |
| 混合：只读事务/零行/文本更新/失败插入 | 0.105/0.083/0.099/0.067s | 0.085/0.074/0.072/0.048s | +11%～+29% 改善 |
| 混合：过滤列更新 | 0.1359s | 0.1458s | **-7.3% 回退** |
| 混合：向量更新 | 0.3328s | 0.3395s | -2.0%（快照重建主导，预期持平） |
| 快照构建次数/逻辑字节 | 9 / 46,800,000 | 9 / 46,800,000 | 不变 |
| 校验 | 全部 ID/距离/重启内容核对通过，两组 Recall@10 = 1.0 | 同 | — |

**如实记录**：容器对比仅 1 轮，不能当作稳定中位数；`filter_update` 轻微回退未在更多轮次中确认是否噪声。原始报告：`cbench-base.json`、`cbench-opt.json`（在 `.local-cross-1794231842-out/`）。

### 剖析依据（优化前，进程内 cProfile）

- 查询路径 400 次：内核扫描 47%、分词器 17%、`storage.get` 族 15%、`convert` 5%。
- 写入路径 100 批：分词器 21%、`convert` 37%（其中双遍有限性检查约 13%）、fsync 3.6%（持久化底线，未动）。

## 回归验证（优化后全绿）

| 套件 | 环境 | 结果 |
| --- | --- | --- |
| `unittest discover`（106 项） | 宿主机 | 通过，24.8s |
| 公开用例（40 项） | 宿主机 | 通过 |
| `p0_transaction_kill`（19）+ `p0_vector_modes`（180）+ `p1_recovery_timing`（22）+ `p2_sql_surface`（166） | 宿主机 | 全部通过 |
| `unittest discover`（106 项） | 新镜像容器 256MiB | 通过，20.9s |
| 公开用例（40 项） | 新镜像容器 | 通过 |
| `p2_sql_surface`（166 项） | 新镜像容器 | 通过（含 65536/65537 维、非法向量、float32 语义） |
| `container_vector_pressure`（64MiB，18,000×1024 维） | 新镜像容器 | 通过，`oom_kill=0` |
| `docker_validation` 生命周期（SIGKILL/EOF/SIGTERM/同卷） | 新镜像 64MiB | 4/4 通过，无 OOM |
| benchmark 内建真值核对 | 两版 | 1e-12 距离容差 + ID 序列全过 |

`tests.validate_submission` 现报 `Source changed: minivectordb/engine.py`——**符合预期**：源码已改而历史报告摘要绑定旧版。本轮结果是新版本的独立记录，不重标任何 `v4-*`/r3 历史报告；提交前须由用户决定是否将本轮修改纳入正式版本。

## 边界与后续

- 1 轮容器对比需补 3 轮中位数才能作为稳定结论；`filter_update` 回退待确认。
- 查询剩余成本：内核扫描（已向量化）+ 通用执行器 Python 全扫描（Python 路径 32s 未改善）。若正式 ann 有 Recall 要求，r3 已声明"本轮不实现 ANN"的边界仍在。
- B（物理 I/O）与 M（容器峰值）未在本轮复测（历史 r3 记录中两者对源码小改不敏感）；正式口径以平台 cgroup 为准。
- 以上分析来自 Agent，不写入学生理解记录。
