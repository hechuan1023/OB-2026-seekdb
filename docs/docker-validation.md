# Linux 容器验证记录

2026-11-09，Docker Desktop 4.46.0 / Docker Engine 28.4.0，Linux x86_64，cgroup v2。运行容器全部禁网；依赖只在构建阶段安装。镜像中 Python 3.11.18，自研原生库 ABI=1。数据库核心未因本轮验证而修改。

## 构建与版本

最初的 gcc:12.2.0 / python:3.11.11-slim-bookworm 拉取因 Docker Hub 连接超时失败。随后使用本机已有且以 digest 固定的官方 Python Bookworm 基础镜像，Debian 源可访问，锁定 32 项编译依赖后从源码构建成功。

- 基础镜像：`python:3.11-slim-bookworm@sha256:90d117a7f1ef6535dc75831e1a9184725757fede0d4d424d7977fb5429d50610`。
- 验证镜像：`minivectordb:round2-validated`。
- 本机 image ID：`sha256:5d89f8491af1115ceeec8f564c00cb3d34f6d23d86f1cde728c2e1133fc4122e`。这是本地镜像标识，未发布镜像仓库；重新构建的 provenance 或时间戳可能产生不同 image ID。
- 原生库 SHA256：`89ae0ee96abf7b077542386ab604dd4d38d46e82f6c514f476b63bcd9c10d2c1`。
- [构建完整输出](docker-build.txt)，[编译依赖版本](../build-dependencies.lock)。

基础镜像和依赖版本已锁定，但不是离线构建包。另一台机器构建时仍须能访问对应镜像与软件源；版本不可用时应显式失败。运行阶段无此网络依赖。

## 验证结果

| 检查 | 配置与结果 | 证据 |
| --- | --- | --- |
| 完整测试集 | 2 CPU、256 MiB、无 swap；64 项全部通过，12.352 秒、无跳过 | [测试输出](docker-tests.txt) |
| 官方首批公开用例 | 5 组、32 条 SQL；类型化执行器及本地 stdio 各执行一遍；检查列名、类型、行数、重复数量、排序和距离容差 | 同上，`tests.test_public_cases` 的 10 项测试 |
| 真正容器入口 | 2 CPU、64 MiB、无 swap；同一 Docker volume 跨 4 个进程检查 COMMIT、失败语句、ROLLBACK、SIGKILL、EOF、SIGTERM | [生命周期报告](docker-lifecycle.json) |
| 超内存数据 | 64 MiB、无 swap；20,000 行、每行 8 KiB 文本；WAL 164,694,642 字节；关闭重开后逐行核对外部降序排序 | [资源输出](docker-resource.txt) |
| 向量完整负载 | 10,000 条、128 维、1,000 次查询、K=10、半数带过滤；种子 20261111；两组召回均 1.0、距离/ID/顺序/数量及重启内容均通过 | [原始报告](docker-benchmark.json) |

完整负载单轮：写入 2.016007 秒，查询 2.028179 秒，索引准备加预热 0.107763 秒，重启至 READY 0.253651 秒。驱动和数据库共用 256 MiB / 2 CPU 容器，种子和历史 Windows 实验不同，没有正式基线、三轮中位数或受控冷热缓存，不据此声称跨系统提速或计算 E。

资源测试的 memory.peak 为 67,108,864 字节，达到 memory.max；memory.events 中 max=6864，oom=0、oom_kill=0。这表明发生了内存限额回收压力并完成测试，并不意味着页缓存不计内存或仍有充足余量。io.stat 保留原始值，但没有正式专用数据设备与统计窗口，因此不称作赛事 B。测试驱动与数据库共享 cgroup 的开销也包含在内。

资源脚本第一次在比较元组与列表时断言失败，首行 ID 为正确的 19999。修正脚本为逐字段比较后，使用全新空目录重测通过；数据库代码未改动。[失败原始输出](docker-resource-first-attempt.txt) 保留供核对。

## 复现命令

在仓库根目录运行。下面为 Bash；Windows PowerShell 将 `$PWD/tests` 等源路径改为绝对 Windows 路径，使用单行命令即可。

```bash
docker build --platform linux/amd64 -t minivectordb:round2-validated .

docker run --rm --network none --cpus=2 --memory=256m --memory-swap=256m \
  -e TMPDIR=/data -v "$PWD/tests:/app/tests:ro" -v "$PWD/mysqltest:/app/mysqltest:ro" \
  --entrypoint python minivectordb:round2-validated -m unittest discover -v

python -m tests.docker_validation --output docs/docker-lifecycle.json

docker run --rm --network none --cpus=2 --memory=64m --memory-swap=64m \
  -e TMPDIR=/data -v "$PWD/tests:/app/tests:ro" \
  --entrypoint python minivectordb:round2-validated -m tests.container_resource_probe

docker run --rm --network none --cpus=2 --memory=256m --memory-swap=256m \
  -e TMPDIR=/data -v "$PWD/tests:/app/tests:ro" -v "$PWD/native:/app/native:ro" \
  -v "$PWD/docs:/results" --entrypoint python minivectordb:round2-validated \
  -m tests.benchmark --seed 20261111 --output /results/docker-benchmark.json
```

镜像的 `/data` 为 Docker volume。`--rm` 清理每次测试新建的匿名数据卷；生命周期脚本创建独有名称的卷并在结束后清理。源码测试通过只读挂载加入，不覆盖镜像中的程序或 Linux 原生库。复测会覆盖指定报告，若需保留历史，应使用不同输出路径。

这些验证不使用组织方完整协议驱动，不代表官方预检、隐藏准入或性能排名通过。
