# OpenMP 精确 TopK 优化记录

日期：2026-11-13

本记录对应测试分支 `perf/iterative-optimization` 中包含本文件、Dockerfile、原生内核和测试的同一个 Git 提交。基线为提交 `a1897ff9bcc7727539579685395e00331253953c`；候选镜像为 `minivectordb:omp-exact-candidate-20261113`，镜像 ID 为 `sha256:ea9eadf37b9f50cf7bab34292945afd5524a54227ea9bd291626892a4e4f5b77`。这些是本地诊断结果，不是官方 T、B、M 或 E。

## 改动

- `native/exact.cpp` 增加 `mv_topk_f32_exact_omp`。无过滤大表按连续区间拆给 2–4 个线程，每段保留 K+1 个候选，合并后按距离和主键排序，再用原来的有序 double 循环计算返回距离。
- `minivectordb/vector_index.py` 从 `/sys/fs/cgroup/cpu.max` 读取可用的 CPU 配额，限制为 1–4 线程。只有无过滤、记录数至少 4096、配额大于 1 时才进入 OpenMP 路径；过滤查询、小表和单 CPU 配额继续使用原单线程路径。
- `Dockerfile` 使用 `-fopenmp -pthread` 编译，并把构建阶段实际使用的 `libgomp.so.1.0.0` 复制到运行镜像，建立 `libgomp.so.1` 链接。
- 新增差分测试覆盖 7/128 维、K=1/10/11、完全并列时主键排序、小表回退，以及“过滤查询绝不误入 OpenMP”的路由约束。

未采用的 NSW 图实验代码已从 `native/exact.cpp` 删除，未进入本提交。

## 正确性验证

| 验证 | 结果 |
| --- | --- |
| Windows 完整回归 | 180 项通过 |
| Docker 2 CPU / 256 MiB 完整回归 | 180 项通过 |
| 官方公开 JSONL 用例 | 20/20 通过 |
| 64 MiB 生命周期 | 读己之写、语句原子性、回滚、确认提交后 SIGKILL、未提交回滚、EOF/SIGTERM 重启全部通过 |
| 64 MiB 超内存压力 | 18,000 条、1,024 维，向量文件 73,728,000 B，`oom=0`、`oom_kill=0`，12.08 秒 |
| 精确结果 | 公开负载有过滤、无过滤 Recall@10 均为 1.0；同进程 A/B 逐条比较 ID、距离和排序一致 |

## 性能结果

### 原生内核同进程 A/B

10,000 条、128 维、K=11、1,000 次查询；同一进程、同一数据和同一缓存，逐查询交替 1/2 线程并比较结果：

| 指标 | 1 线程 | 2 线程 | 变化 |
| --- | ---: | ---: | ---: |
| 总耗时 | 0.13256 s | 0.10126 s | -23.61% |
| 中位耗时 | 115.27 µs | 78.38 µs | -32.00% |
| 逐查询中位比值 | — | 0.7378 | -26.22% |
| 2 线程更快 | — | 908/1,000 | — |

### 容器内 JSONL 交替 A/B

两组各 7 对、合计 14 对；数据库子进程和驱动位于同一 2 CPU cgroup，每对使用独立数据目录，逐条核对独立真值：

| 指标 | 中位比值变化 | 候选更快对数 |
| --- | ---: | ---: |
| 全部查询 | -16.65% | 11/14 |
| 过滤查询 | -8.94% | 10/14 |
| 无过滤查询 | -19.38% | 12/14 |

过滤查询代码路径未切换到 OpenMP，上表的过滤差异包含宿主调度和管道噪声，不能解释为线程收益。无过滤结果与原生内核 A/B 方向一致。

### 宿主侧 JSONL 交替 A/B

同一负载再以宿主驱动、Docker 标准输入输出跑 14 对：

| 指标 | 中位比值变化 | 候选更快对数 |
| --- | ---: | ---: |
| 全部查询 | +0.02% | 7/14 |
| 过滤查询 | +2.49% | 4/14 |
| 无过滤查询 | -2.18% | 10/14 |
| 峰值内存 | +0.23% | — |
| `/data` 写入 | 6,045,696 B，双方相同 | — |

宿主当时同时运行多个无关容器，端到端管道波动明显；这组结果说明“官方端到端收益仍需正式环境验证”，不能据此宣称稳定加速。内核和容器内 A/B 只用于判断实现是否有效。

## 复现

```bash
docker build -t minivectordb:omp-exact-candidate-20261113 .

docker run --rm --entrypoint python3 --cpus=2 --memory=256m \
  --memory-swap=256m --network none \
  -v "$PWD/tests:/app/tests:ro" \
  -v "$PWD/mysqltest:/app/mysqltest:ro" \
  -v "$PWD/protocol_check:/app/protocol_check:ro" \
  -w /app minivectordb:omp-exact-candidate-20261113 \
  -m unittest discover -q
```

官方公开协议：

```bash
docker run --rm \
  -v "$PWD/.local-protocol-runs:/out" \
  -v "$PWD/protocol_check:/check:ro" \
  -v "$PWD/mysqltest:/pkg:ro" \
  -w /app --entrypoint python3 \
  minivectordb:omp-exact-candidate-20261113 \
  /check/run.py \
  --command '["python3","-m","minivectordb","--wire","jsonl","--data-dir","{data_dir}"]' \
  --package /pkg --output /out/run1
```

## 取舍与限制

- 该方案只并行无过滤 exact 扫描；带过滤查询仍先得到候选位置，再走原单线程内核，避免改变过滤、事务和排序语义。
- 1 CPU、缺少 `cpu.max`、小表或旧原生库都会安全回退，不依赖多线程才能工作。
- OpenMP 增加了 `libgomp` 运行时文件和少量峰值内存；当前容器内峰值内存约 103 MiB，宿主侧 JSONL 对比只增加约 0.23%。
- 每次查询仍有 OpenMP 区域启动和候选合并开销，因此大表收益明显，小表没有启用。
- 正式 CPU 配额、冷缓存、驱动效率和 I/O 统计仍以平台为准。本记录不能替代正式准入或性能排名。
