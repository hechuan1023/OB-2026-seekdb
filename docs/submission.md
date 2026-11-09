# 交付清单与后续提测

目标仓库：https://github.com/hechuan1023/OB-2026-seekdb

分支：`2026-competition-round2`。官方公开用例来源提交：`9f6df2ba5acf871376e22b64a3d0724e4b575044`。实际提交完整 SHA 必须从包含本次源码和材料的提交取得，不能使用官方基础 SHA：

```bash
git rev-parse HEAD
git status --short
git ls-remote submission refs/heads/2026-competition-round2
python -m tests.validate_submission --git
```

本文件与当前源码一同纳入 Git；不在提交内容中写入它自身的 commit SHA，以免产生循环依赖。源码字节摘要见 source-manifest.json，材料中的 version 与其一致。

本轮基于 `5d78f92a21418340a95331e8ae3eed6439bd0da5` 合入官方公开 v4 题包，补齐本地测试适配器。20 组、95 条 SQL 两种路径全部通过；Windows/Linux 各 106 项回归和真实容器恢复通过，详见 [v4 验证与复盘](public-v4-validation.md)。性能报告保留为原版本历史结果，本轮不宣称性能提升。

| 产物 | 位置与状态 |
| --- | --- |
| 自研数据库 | minivectordb/、native/exact.cpp；SQL、存储、执行、事务、恢复与精确向量内核 |
| 从源码构建 | Dockerfile、build-dependencies.lock、.dockerignore、pyproject.toml；Linux 镜像已构建 |
| 运行与限制 | README.md；含入口、/data、SQL 示例、提交回滚、重启和临时协议 |
| 赛题与设计 | docs/minivectordb-requirements.md、docs/design.md |
| 验证与复盘 | docs/docker-validation.md、docs/validation.md、docs/retrospective.md；原始日志与 JSON 报告随附 |
| 官方公开样例 | mysqltest/；原样保留，执行适配器为 tests/test_public_cases.py；tests/run_public_cases.py 生成报告 |
| 学习材料 | 单个 agent/conversation.json；UTF-8 JSON 对象、低于 128 KiB、无重复键/非标准数值 |
| 版本核对 | docs/source-manifest.json；校验器检查工作区摘要、材料版本及已验证报告；--git 另外核对提交中的文件字节 |

## 尚不能宣称完成的事项

1. 官方 v4 公开 20 组、95 条 SQL 及两种驱动指令已取得，但完整进程协议、错误码、exact/ann 参数、退出约定、正式资源配置、性能基线及完整驱动仍未包含在已核对官方提交中。现有 READY/TSV 是本地协议。
2. 用户此前反馈正式提测未通过，仅有汇总而无具体错误，失败原因未确认。本轮仅执行本地验证，没有调用正式提测或消耗新机会。
3. ANN 模式当前走精确 flat 扫描。是否满足进阶索引的全部正式约束，需要对照题包；尚无自研近似索引。
4. conversation.json 真实保留用户请求、Agent 实现决策与实验结果。学生本人尚无技术解释、预测和实验分析的充分证据，不能把 Agent 的解释当作学生贡献，也不能承诺该项得分。

## 题包补齐后的顺序

先确认 OPEN 清单及协议，替换隔离的适配层；运行官方公开驱动、协议检查器与故障示例。然后按冻结资源配置做三轮 benchmark，核对两组召回、持久化、I/O 和内存；再生成新源码摘要和真实实验材料、提交新 commit、完成预检。最后按平台流程确认正式提测并锁定同一 commit、镜像和材料摘要。

正式提测只提供仓库 HTTPS URL、上述分支及最终完整 40 位 SHA；代码、Dockerfile 和材料必须来自这个提交。不要拼接不同版本的性能报告与材料成绩。
