# 公开 v4 题包验证与复盘

日期：2026-11-09。本轮由 Agent 执行和解释，用户要求同步题包、修复问题、验证、更新材料并提交推送；不将下文技术解释归为学生本人理解。

## 结果与边界

官方来源为 `9f6df2ba5acf871376e22b64a3d0724e4b575044`（北京时间 18:40:21，`upload more cases`），题包版本 `minivectordb-public-v4`。整个 `mysqltest/` 原样合入，20 组共 95 条 SQL，按 manifest 顺序执行，每组使用独立空目录。

| 实际验证 | 结果 |
| --- | --- |
| Linux 公开用例：类型化执行 + 本地 stdin/stdout | 40 项通过，4.998 秒；每条路径 95 条 SQL、7 次正常重启、1 次 ann 切换、4 个预期错误 |
| Linux 完整回归 | 106 项通过，22.885 秒，无跳过；2 CPU、256 MiB、无 swap、断网，临时数据在 /data |
| Windows 完整回归 | 106 项通过，41.958 秒，无跳过 |
| 真实镜像 ENTRYPOINT 与生命周期 | 64 MiB、无 swap、断网；SIGKILL、EOF、PID 1 SIGTERM 与同卷重启全部通过，无 OOM |
| 镜像核对 | Python 源码逐文件 SHA256 一致，自研原生库 ABI=1 |

镜像：`minivectordb:public-v4`，ID `sha256:e125eda8d833fbf38d7fc0bd63c748393d51ee6a27731ae73bd09bf6b4370038`。使用现有固定基础镜像和依赖锁构建，运行实现来自镜像，仅挂载测试脚本和官方用例；没有用主机源码覆盖镜像内实现。

原始证据：[公开用例报告](public-v4-retest.json)、[Linux 完整输出](v4-linux-full-tests.txt)、[Windows 完整输出](v4-windows-tests.txt)、[实际容器恢复](v4-lifecycle.json)。这些是本地自测，不是组织方驱动或正式准入结果。

用户此前反馈正式提测只显示 MySQL 未通过，无具体错误；该反馈真实保留，无法确认失败阶段、对应锁定 SHA 或根因。本轮未调用正式提测，不消耗正式机会，也未重新测量性能。

## 实际发现和修正

同步题包后，先用旧适配器执行两项代表测试：索引维护失败于 `AssertionError: 11 != 9`，因为把两条驱动指令也计为 SQL；重复主键测试把正确发生的 `DBError: Duplicate primary key` 当成测试错误。两项分别为 1 failure、1 error。这些是测试适配器缺口，不能误判为数据库不支持索引或主键约束。

修正如下：

- 按 manifest 中 test/result 路径加载，先核对 SQL 数量与断言数量；`--restart` 和 `--mode` 不消费断言。未知指令立即失败，不能静默跳过。
- `--restart` 通过 EOF 正常关闭、等待零退出码，再复用原目录启动；不清空文件，检查 stderr，关闭时保留数据库原有的未提交回滚行为。
- 错误语义显式映射为本地错误码：`duplicate_key → CONSTRAINT_ERROR`、`dimension_mismatch → INVALID_VECTOR`、`unsupported_sql → UNSUPPORTED`。错误后继续后续 SQL；不接受成功、无错误码或无关错误。该映射不是官方冻结错误码。
- 添加默认关闭的 `--local-mode-controls`。仅在独立完整 SQL 之间识别本地 `--mode exact/ann` 控制行，保留同一进程及活动事务；普通默认 SQL 注释行为不变。不能用重新启动实现模式切换，否则会把活动事务回滚、掩盖问题。
- `tests/run_public_cases.py` 按原始清单顺序运行并输出结构化报告，保留路径、计数、结果和源码摘要；材料校验器逐文件比对官方题包，防止误改输入或期望值。

补齐适配器后全部公开断言通过，本轮未发现需要修改存储、事务、SQL 执行或向量内核的功能错误。核心引擎未改动；修改局限于本地协议控制、测试和交付材料。

交付校验还发现 Windows 自动 CRLF 换行导致题包字节摘要不一致（`Public package modified: mysqltest/README.md`）。逐文件核对确认 42 个文件均仅有换行差异；增加 `/mysqltest/** -text` 并恢复官方 Git blob 原始字节后，重新执行全部 40 项公开检查，4.998 秒通过。

另增加 4 项独立适配器测试：事务内切换模式不丢修改且可回滚、ann 下重启仍恢复未提交删除、成功/错误码不匹配不得通过、未知指令与断言错位必须失败、模式控制默认关闭且不解释字符串中的控制样文本。这些检查归入 4 个 unittest 测试方法，其中事务状态场景分别验证两条路径。

## 复现

```bash
docker build --platform linux/amd64 -t minivectordb:public-v4 .
docker run --rm --network none --cpus=2 --memory=256m --memory-swap=256m \
  -e TMPDIR=/data -v "$PWD/tests:/app/tests:ro" -v "$PWD/mysqltest:/app/mysqltest:ro" \
  -v "$PWD/docs:/reports" --entrypoint python minivectordb:public-v4 \
  -m tests.run_public_cases --output /reports/public-v4-retest.json
docker run --rm --network none --cpus=2 --memory=256m --memory-swap=256m \
  -e TMPDIR=/data -v "$PWD/tests:/app/tests:ro" -v "$PWD/mysqltest:/app/mysqltest:ro" \
  --entrypoint python minivectordb:public-v4 -m unittest discover -v
python -m tests.docker_validation --image minivectordb:public-v4 --output docs/v4-lifecycle.json
python -m tests.validate_submission --git
```

## Agent 复盘

1. 公开题包更新的是测试语言及覆盖范围，不只是更多 SQL。先观察旧适配器的失败，再修正指令和错误处理，可以避免为了“让测试通过”而改坏原本正确的引擎。
2. 测试驱动本身需要反例。若任何错误都接受、模式切换偷偷重启、驱动指令吃掉断言，绿色结果也可能无效。本轮加入了会直接揭示这些问题的检查。
3. 官方基础样例被简化，因此测试组数增加不等于每个旧边界都被覆盖；完整自研回归仍保留 Unicode 边界、排序并列、非法向量、失败原子性、强杀与重复恢复等检查。
4. 公开样例、完整本地回归、实际镜像生命周期是不同层次的证据。新 README 说明正式共 29 个用例；本地通过不能推导其余隐藏输入或正式协议已通过。
5. 协议仍是交付风险：官方仍不附完整运行驱动。`READY/TSV`、本地模式控制和错误码映射都必须明确标为开发约定；不能靠猜测改成 JSON 或 TCP。
6. 本轮不涉及性能优化，未重跑三轮 benchmark；旧报告回归历史版本关联。学习材料只追加真实用户请求、实际结果与 Agent 解释，压缩重复历史版本摘要以控制 128 KiB，不创造学生发言或正式成绩。
