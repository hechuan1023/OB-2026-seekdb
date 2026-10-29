# AMD × hipVS 赛题：合规提测与优化作战手册

> 更新：下文为历史操作指南。round2 的 75 次方案见 [AMD-CANDIDATE-75.md](AMD-CANDIDATE-75.md)。
> `amd-cagra-opt-round7` 使用 64/64/8 建图与 4096 候选 MULTI_CTA 搜索。
> 开发测量已超过 3 倍构建满分线，正式成绩仍需提测确认；最新记录见 [AMD-CANDIDATE-ROUND7.md](AMD-CANDIDATE-ROUND7.md)。
> round6 的测试、内存和 profiler 记录见 [AMD-CANDIDATE-ROUND6.md](AMD-CANDIDATE-ROUND6.md)。
> round5 的验证记录见 [AMD-CANDIDATE-ROUND5.md](AMD-CANDIDATE-ROUND5.md)。
> 快照兼容边界见 [AMD-CANDIDATE-ROUND4.md](AMD-CANDIDATE-ROUND4.md)，
> round3 的测试记录见 [AMD-CANDIDATE-ROUND3.md](AMD-CANDIDATE-ROUND3.md)。
> 不要将下文早期保底得分或逐位一致的描述视为验证结论。

> 适用赛题：使用 hipVS 加速 HNSW 索引构建（AMD 赛题，总分 20 分 = 硬门槛 4 + 构建性能 14 + 批量查询 2）
> 分支：`amd-cagra-submit`（父提交 = V5 冻结 commit，满足评测的「冻结基线后代」校验）
> 冻结基线：仓库 `https://github.com/zihaomu/seekdb`，分支 `amd-oceanbase-official-contest-base-v5`，
> Tag `amd-cagra-contest-baseline-v5`，commit `634888d9b037153fa143d2c913ba4b25503e1d62`

---

## 0. 之前两次「执行失败」的根因

题面第十节给出了评测失败原因码，两次失败都不是代码问题：

| failMsg | 含义 | 我们是否命中 |
|---|---|---|
| 1 | 指定分支不存在 | 第一次命中（填了远端没有的 `feature/sdd-context-manager`） |
| 2 | 指定 commit 不存在 | — |
| 3 | 编译失败 / 未通过 GPU 路径抽检 | — |
| **4** | **git clone 失败：仓库地址不是 `https://<用户名>:<令牌>@github.com/<所有者>/<仓库>` 格式，或令牌无效** | **命中** |
| 5 | git clone 超时 | — |
| 6 | 检出指定版本失败 | — |
| 7 | CMake 失败 | — |
| **8** | **验证失败：提交不是 V5 冻结 commit 的后代** | **命中**（我们基于 `oceanbase/seekdb@2026-competition`，与 V5 无祖先关系） |

关键点（题面 4.1 节原文）：

> 只接受 `https://<用户名>:<令牌>@github.com/<所有者>/<仓库>` 这一种格式。地址不符合该格式时，
> **评测系统不会尝试拉取代码，直接回传评测失败（failMsg = 4）**。
> 以下地址会被拒绝：不带令牌的 `https://github.com/...`、SSH 地址、非 GitHub 地址、仓库内的子路径。

所以「不带令牌的公开 GitHub 地址」和「不是 V5 后代的提交」这两条，任何一条都足以让本次提测 0 分，
和代码写得好不好无关。

---

## 1. 提测前置合规（必须先做完，否则一定 0 分）

### 1.1 建一个私有答题仓库

在 GitHub 上新建 **Private** 仓库（建议名 `seekdb-2026`），两种做法任选：

- **A（省事）**：GitHub 右上角 `Import Repository`，填 `https://github.com/zihaomu/seekdb`。
  导入后仓库里会直接带上 `amd-oceanbase-official-contest-base-v5` 分支和 V5 的完整历史。
- **B（按题面写法）**：`Import Repository` 填 `https://github.com/oceanbase/seekdb`，
  再把 V5 分支 fetch 过来：

  ```bash
  git clone https://github.com/<你的账号或团队>/seekdb-2026.git
  cd seekdb-2026
  git remote add contest-base https://github.com/zihaomu/seekdb.git
  git fetch contest-base amd-oceanbase-official-contest-base-v5 --tags
  git switch -c amd-cagra-submit contest-base/amd-oceanbase-official-contest-base-v5
  ```

### 1.2 生成只读令牌

GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens**：

- Repository access：只勾选本题的答题仓库
- Permissions：`Contents: Read-only`（不要给任何写权限）
- 有效期：覆盖比赛及成绩处理期（比赛结束后 11 天），赛后自行撤销
- **不要**把令牌写进代码、提交记录、Issue 或群里

### 1.3 把参赛信息里的仓库地址改成合规格式

打开 https://open.oceanbase.com/competition → 你的参赛信息 → seekdb / AMD 题的仓库地址，改成：

```
https://<你的GitHub用户名>:<只读令牌>@github.com/<仓库所有者>/<仓库名>
```

注意：**不带令牌会被判 failMsg=4**；仓库必须是私有库；地址里不能带子路径（不要 `.../tree/main`）。

> 如果参赛信息页已经关闭/不可编辑，参见第 6 节「找官方改登记」的论坛发帖模板。

### 1.4 把提交分支推进那个仓库

```bash
git push <你的私有仓库> amd-cagra-submit:amd-cagra-submit
```

### 1.5 提测表单填写

| 字段 | 填什么 |
|---|---|
| 仓库地址 | 参赛信息里登记的那个带令牌的地址（必须与登记一致） |
| 分支 | `amd-cagra-submit` |
| Commit | 该分支的 **tip SHA**（平台是 `--depth 1` 浅克隆，只能 checkout 分支 tip） |

```bash
# 取当前 tip
git rev-parse amd-cagra-submit
```

---

## 2. 评测的固定编译与测试方式（不可改）

题面 3.2 / 6.2 / 7 节，评测系统会在答题仓库根目录依次执行：

```bash
# 1. 编译 hipVS bridge（脚本在答题仓库里，可以改）
HIPVS_ROOT=/opt/hipvs ROCM_ROOT=/opt/rocm \
  tools/hipvs_bridge/build.sh <输出目录>/libseekdb_cuvs_bridge.so

# 2. 编译 seekdb（命令和参数固定，不能追加）
cmake -S . -B build_release -G "Unix Makefiles" \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo \
  -DOB_ENABLE_UNITY=ON \
  -DOB_USE_LLD=ON \
  -DOB_BUILD_CUVS=ON \
  -DOB_BUILD_CUVS_TRACE=OFF \
  -DCUVS_BRIDGE_LIB=<输出目录>/libseekdb_cuvs_bridge.so
cmake --build build_release --target seekdb
```

评测环境：ROCm 7.2.4 / GCC 13.3 / CMake 3.31 / Rust 1.97，hipVS 在 `/opt/hipvs`（只读，固定 commit
`f96a663d0a4441d6f5344bd3ef552b71ae7b370b`），**编译环境不联网**，`deps/3rd` 会被替换成冻结版本。

被测 SQL 与参数（固定，不可改）：

```sql
CREATE VECTOR INDEX idx_items ON items(v)
  WITH (distance=l2, type=hnsw, lib=cuvs, m=16, ef_construction=200, ef_search=64);

SELECT id FROM items ORDER BY l2_distance(v, <query>) APPROXIMATE LIMIT <k>;
```

数据集：`SIFT250K`（250K × 128，权重 40%）、`Hidden250K`（250K × 256，权重 60%），FP32 + L2。

---

## 3. 本次仓库里做了什么

### 3.1 分支

`amd-cagra-submit` 的父提交就是 V5 冻结 commit `634888d9`，因此评测的「冻结基线后代」校验天然通过：

```bash
git merge-base --is-ancestor 634888d9b037153fa143d2c913ba4b25503e1d62 amd-cagra-submit && echo OK
```

### 3.2 `tools/hipvs_bridge/seekdb_cuvs_bridge.c`：CAGRA 构建参数运行时可调

V5 基线把 CAGRA 构建参数写死为：

```c
h->ip->build_algo = NN_DESCENT;
h->ip->graph_degree = 128;
h->ip->intermediate_graph_degree = 256;
h->ip->nn_descent_niter = 100;
```

而 cuVS CAGRA 的官方默认是 `64 / 128 / 20`——基线取值远高于默认，这正是构建时间的主要来源。

本分支把参数改为**运行时从环境变量读取**，并且**默认值与 V5 完全一致**，所以：

- 不设任何环境变量 → 行为与数值与冻结基线逐位一致（保底 4 分的那一版）
- 设环境变量 → 同一份编译产物即可在云端 A/B 多种参数，无需反复重编译

| 环境变量 | 默认（= 基线） | 说明 |
|---|---|---|
| `OB_CUVS_PROFILE` | `baseline` | `fast` = 64/128/30，`aggressive` = 64/128/20 |
| `OB_CUVS_GRAPH_DEGREE` | `128` | 最终图度数，越小建图越快、召回略降 |
| `OB_CUVS_INTERMEDIATE_GRAPH_DEGREE` | `256` | 中间图度数 |
| `OB_CUVS_NN_DESCENT_NITER` | `100` | NN-descent 迭代次数，**对构建时间最敏感** |
| `OB_CUVS_BUILD_ALGO` | `nn_descent` | 可切 `iterative_search` |
| `OB_CUVS_ITOPK_SIZE` | 不设置（用库默认） | 构建期候选集大小 |
| `OB_CUVS_SEARCH_WIDTH` | `4` | 查询期宽度（影响召回与查询吞吐） |

另外构建完成时会打印一行，便于取数：

```
[seekdb_cuvs] build done n=250000 dim=128 algo=0 degree=128 inter=256 niter=100 itopk=0 elapsed=23.412s
```

> 说明：改动只涉及新增 `#include <string.h> <time.h>`、参数解析函数与一行日志，
> V5 原本的默认值与行为路径保持不变；`hipcc` 编译所需的 hipVS 头文件完全没动。

### 3.3 `docs/gpu-vector-index-hipvs-cuvs/validation/amd_cagra_bench.py`：A/B 测量脚本

对着**已经启动的 observer**跑，测的正是评测计分的那几项：

- `CREATE VECTOR INDEX ... lib=cuvs` 的耗时（构建窗口）
- 第一条固定查询耗时（计入构建窗口）
- nq 条单行 `APPROXIMATE` 查询的 QPS / P95
- Recall@10 / Recall@100（与 numpy 暴力精确近邻对比）

输出 `<evidence>/<label>.json`，多组参数直接横向对比。

---

## 4. 云端 A/B 流程（拿到 Radeon Cloud 实例后照做）

1. **领算力**：注册 AMD 开发者计划（ADP）→ 登录 Radeon Cloud 完成验证 → 兑换算力券 →
   `AMD 开发者云` 用 GitHub 账号登录 → Profile → Redeem Credits 粘贴券码。
   热门时段 07:00–23:00 每 48 小时一轮分配，非热门时段 23:00–07:00 先到先得；单实例最长 4 小时。
2. **起实例**：Gallery 搜 `seekdb`，选 `seekdb-hipvs` 模板 Launch，Open Notebook。
3. **拉我们的分支并编译**（在 Notebook 终端里，root）：

   ```bash
   cd /workspace
   git clone https://github.com/hechuan1023/OB-2026-seekdb.git cand
   cd cand && git checkout amd-cagra-submit
   HIPVS_ROOT=/opt/hipvs ROCM_ROOT=/opt/rocm \
     tools/hipvs_bridge/build.sh /workspace/bridge/libseekdb_cuvs_bridge.so
   cmake -S . -B build_release -G "Unix Makefiles" \
     -DCMAKE_BUILD_TYPE=RelWithDebInfo -DOB_ENABLE_UNITY=ON -DOB_USE_LLD=ON \
     -DOB_BUILD_CUVS=ON -DOB_BUILD_CUVS_TRACE=OFF \
     -DCUVS_BRIDGE_LIB=/workspace/bridge/libseekdb_cuvs_bridge.so
   cmake --build build_release --target seekdb
   ```

4. **每一组参数都要重启 observer**（环境变量在 observer 进程里读取），再跑测量：

   ```bash
   export OB_CUVS_GRAPH_DEGREE=64 OB_CUVS_INTERMEDIATE_GRAPH_DEGREE=128 OB_CUVS_NN_DESCENT_NITER=30
   # 用上面 build_release/src/observer/seekdb + 新 bridge 启动 observer
   python3 cand/docs/gpu-vector-index-hipvs-cuvs/validation/amd_cagra_bench.py \
     --rows 250000 --dim 128 --nq 4096 --label fast-64-128-30 --evidence /tmp/amd-ab
   ```

5. **对比各组结果**，用「构建时间更短且 Recall 不低于基线 0.005」这一条筛选
   （评测硬门槛：Recall@10/100 相比同轮冻结基线下降不得超过 0.005）。
6. **把胜出参数回填成默认值**（改 `sk_resolve_params()` 里 baseline 的取值），
   再提测——评测机上不会设任何环境变量，最终生效的是代码里的默认值。

---

## 5. 打分换算（题面第九节）

```
综合构建加速比 = SIFT250K 加速比^0.4 × Hidden250K 加速比^0.6
构建性能分 = 14 × clamp( ln(综合构建加速比) / ln(3.0), 0, 1 )
查询性能分 =  2 × clamp( ln(综合查询加速比)  / ln(1.5), 0, 1 )
总分 = 4 + 构建性能分 + 查询性能分   （任一硬门槛失败 → 0）
```

即：1.2x ≈ 2.3 分、1.5x ≈ 5.2 分、2.0x ≈ 8.8 分、≥3.0x 拿满 14 分；不设任何优化直接提交冻结基线 = 4 分。

参考：V5 基线在评测机（AMD Radeon PRO W7900D，ROCm 7.2.4）上的构建时间是
SIFT250K 20~27 s、Hidden250K 29~35 s（含第一条查询）。

---

## 6. 如果参赛信息页改不了（找官方改登记）

论坛：https://ask.oceanbase.com/c/competition/10-category/10

```
标题：AMD 赛题提测 1 秒执行失败（failMsg=4），请求协助更新登记仓库地址

正文：
- 赛题：AMD（使用 hipVS 加速 HNSW 索引构建）
- 现象：提测立即「执行失败」，成绩 0；同一仓库同一分支 commit 用于 seekdb 题可正常判分
- 定位：按《AMD 赛题说明》4.1 节，答题仓库地址只接受
  https://<用户名>:<令牌>@github.com/<所有者>/<仓库> 格式，不带令牌的地址直接判 failMsg=4，
  评测系统不会尝试拉取代码；我们报名登记的是不带令牌的 https://github.com/... ，报名表已关闭无法自助修改
- 已就绪：分支 amd-cagra-submit，父提交为 V5 冻结 commit 634888d9（保证通过「冻结基线后代」校验）
- 请求：能否由后台把本队 AMD 题的登记仓库地址更新为带只读令牌的 GitHub 私有库地址
```

---

## 7. 风险清单

| 风险 | 应对 |
|---|---|
| 登记地址不带令牌 → failMsg=4 | 先完成 1.1~1.3，**提测前先确认参赛信息里已改** |
| 提交不是 V5 后代 → failMsg=8 | 本分支父提交就是 V5 冻结 commit，`git merge-base --is-ancestor` 自查 |
| 改了参数导致 Recall 下降 → 硬门槛失败 0 分 | 必须先在云端 A/B 验证 Recall，再回填默认值 |
| 编译失败 → failMsg=3 | 改动仅限 bridge 的参数解析与日志；`hipVS` 头文件与 `deps/3rd` 一律未动 |
| 评测机不联网 | 不能引入新依赖；新代码必须以源码形式放进仓库（本改动无新依赖） |
| 仓库太大导致 clone 超时 → failMsg=5 | 本分支树与冻结基线同量级（V5 树 8998 文件 / 298 MB） |
