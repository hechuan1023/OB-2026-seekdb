# 交付清单与后续提测

2026-11-10 更新：官方提交 `f4d4e26653af823a7601668802f80e374ad6643c` 已公开 JSONL 通信协议和自测工具，Docker 默认入口已适配；WSL 下官方检查器针对 Docker 镜像通过握手和 20 组公开用例。下方关于协议缺失的说明为此前状态；正式隐藏用例、性能评测及冻结错误码仍未验证。

目标仓库：https://github.com/hechuan1023/OB-2026-seekdb

分支：`2026-competition-round2`。官方公开用例来源提交：`9f6df2ba5acf871376e22b64a3d0724e4b575044`。实际提交完整 SHA 必须从包含本次源码和材料的提交取得，不能使用官方基础 SHA：

```bash
git rev-parse HEAD
git status --short
git ls-remote submission refs/heads/2026-competition-round2
# 新克隆可能没有上游提交对象；历史报告及题包字节校验需要先取得官方分支。
git fetch https://github.com/oceanbase/seekdb.git 2026-competition-round2
python -m tests.validate_submission --git
```

本文件与当前源码一同纳入 Git；不在提交内容中写入它自身的 commit SHA，以免产生循环依赖。源码字节摘要见 source-manifest.json，材料中的 version 与其一致。

当前版本基于官方 `f4d4e26653af823a7601668802f80e374ad6643c` 的公开协议，官方驱动对实际镜像的握手和 20 组用例通过；Windows/Linux 完整回归各 180 项通过。按 cgroup CPU 配额启用的双线程 exact TopK 的内核、容器和宿主侧对照及限制见 [OpenMP 优化记录](openmp-exact-20261113.md)。历史性能报告保留为原版本结果，本轮不宣称官方性能提升。

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

1. 已取得 JSONL 协议、公开驱动及其当前资源配置；完整错误码映射、隐藏用例、性能基线及正式平台结果仍需确认。READY/TSV 是另行保留的本地开发协议。
2. 用户此前反馈正式提测未通过，仅有汇总而无具体错误，失败原因未确认。本轮仅执行本地验证，没有调用正式提测或消耗新机会。
3. ANN 模式当前走精确 flat 扫描。是否满足进阶索引的全部正式约束，需要对照题包；尚无自研近似索引。
4. conversation.json 真实保留用户请求、学生对语句原子性与事务的预测和理由、Agent 的解释及实际实验结果；学生的实验边界复盘仍未完成。不能把 Agent 的解释当作学生贡献，也不能承诺该项得分。

## 题包补齐后的顺序

在提交前核对源码摘要、真实学习材料和远程提交，完成平台预检；再按平台流程确认正式提测。后续按冻结资源配置做三轮 benchmark，核对两组召回、持久化、I/O 和内存，不能将本地结果当作赛事成绩。

正式提测只提供仓库 HTTPS URL、上述分支及最终完整 40 位 SHA；代码、Dockerfile 和材料必须来自这个提交。不要拼接不同版本的性能报告与材料成绩。
