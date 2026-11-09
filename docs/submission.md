# 交付清单与后续提测

目标仓库：https://github.com/hechuan1023/seekdb

分支：`2026-competition-round2`。官方公开用例来源提交：`a3dba559c1e7ebdc48bc990ffb1a4c75d1c1075c`。实际提交完整 SHA 必须从包含本次源码和材料的提交取得，不能使用官方基础 SHA：

```bash
git rev-parse HEAD
git status --short
git ls-remote fork refs/heads/2026-competition-round2
python -m tests.validate_submission --git
```

本文件与当前源码一同纳入 Git；不在提交内容中写入它自身的 commit SHA，以免产生循环依赖。源码字节摘要见 source-manifest.json，材料中的 version 与其一致。

本轮在 `491f7611579dfc1f52e054dc376c22f89e72394e` 基础上优化索引失效并更新验证。最新版三轮容器对照、72 项测试及大向量资源验证在 [r3-optimization.md](r3-optimization.md)。source-manifest.json 区分当前报告与历史报告，历史报告核对其原提交，不能冒充当前源码成绩。

| 产物 | 位置与状态 |
| --- | --- |
| 自研数据库 | minivectordb/、native/exact.cpp；SQL、存储、执行、事务、恢复与精确向量内核 |
| 从源码构建 | Dockerfile、build-dependencies.lock、.dockerignore、pyproject.toml；Linux 镜像已构建 |
| 运行与限制 | README.md；含入口、/data、SQL 示例、提交回滚、重启和临时协议 |
| 赛题与设计 | docs/minivectordb-requirements.md、docs/design.md |
| 验证与复盘 | docs/docker-validation.md、docs/validation.md、docs/retrospective.md；原始日志与 JSON 报告随附 |
| 官方公开样例 | mysqltest/；原样保留，执行适配器为 tests/test_public_cases.py |
| 学习材料 | 单个 agent/conversation.json；UTF-8 JSON 对象、低于 128 KiB、无重复键/非标准数值 |
| 版本核对 | docs/source-manifest.json；校验器检查工作区摘要、材料版本及已验证报告；--git 另外核对提交中的文件字节 |

## 尚不能宣称完成的事项

1. 官方首批公开 SQL 已取得，但完整进程协议、错误码、exact/ann 参数、退出约定、正式资源配置、性能基线及完整驱动仍未包含在已核对官方提交中。现有 READY/TSV 是本地协议。
2. 没有参加正式预检或正式提测，没有消耗每日 2 次正式机会，没有赛事分数或排名。
3. ANN 模式当前走精确 flat 扫描。是否满足进阶索引的全部正式约束，需要对照题包；尚无自研近似索引。
4. conversation.json 真实保留用户请求、Agent 实现决策与实验结果。学生本人尚无技术解释、预测和实验分析的充分证据，不能把 Agent 的解释当作学生贡献，也不能承诺该项得分。

## 题包补齐后的顺序

先确认 OPEN 清单及协议，替换隔离的适配层；运行官方公开驱动、协议检查器与故障示例。然后按冻结资源配置做三轮 benchmark，核对两组召回、持久化、I/O 和内存；再生成新源码摘要和真实实验材料、提交新 commit、完成预检。最后按平台流程确认正式提测并锁定同一 commit、镜像和材料摘要。

正式提测只提供仓库 HTTPS URL、上述分支及最终完整 40 位 SHA；代码、Dockerfile 和材料必须来自这个提交。不要拼接不同版本的性能报告与材料成绩。
