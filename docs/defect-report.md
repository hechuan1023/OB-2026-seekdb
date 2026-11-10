# 缺陷报告：已推送版本中的两个 P0 问题

日期：2026-11-10。发现方式：并行会话新增的回归测试，在干净副本（`git archive HEAD`）上复现确认。影响版本：HEAD `83d1ae0d2`（已推送 `submission`）。修复状态：**工作区已修复，未提交**。两个缺陷都可能让正式评测的单个用例失败，而功能准入是一票否决（任一用例失败总分为 0）。

## DEF-1：VARCHAR 列与超长字面量比较被错误拒绝

| 项目 | 内容 |
| --- | --- |
| 严重度 | P0（语义错误，隐藏用例可直接触发） |
| 影响版本 | ≤ `83d1ae0d2` |
| 修复位置 | 工作区 `minivectordb/engine.py:197-210`（`conditions()` 对 VARCHAR 只校验字面量类型，不再套用列宽） |
| 回归测试 | `tests/test_engine.py:15` `test_long_varchar_predicates_do_not_enforce_column_width` |

最小复现：

```sql
CREATE TABLE t (id BIGINT PRIMARY KEY, s VARCHAR(2));
INSERT INTO t VALUES (1,'ab');
SELECT id FROM t WHERE s='abc';
```

- 实际：`ERROR	TYPE_ERROR	s exceeds VARCHAR(2)`
- 期望：成功执行并返回空结果集。列宽约束的是**存储值**，不是比较操作数；标准 SQL 中 `VARCHAR(2)` 列与 3 字符字面量比较是合法表达式，只是不匹配。

根因：`conditions()` 对每个 WHERE 字面量调用 `convert()`，而 `convert()` 对 VARCHAR 执行 `len(value) <= size` 的存储校验；存储校验被错误地复用到了谓词上。

影响面：

- 任何 `WHERE varchar_col = '<超长>'` 以及 `<`、`<=`、`>`、`>=` 谓词；
- `UPDATE ... WHERE`、`DELETE ... WHERE` 同样触发；
- 公开题包未覆盖该写法；隐藏用例若用超长字面量过滤会直接判错。

## DEF-2：深嵌套 JSON 向量字面量导致进程崩溃

| 项目 | 内容 |
| --- | --- |
| 严重度 | P0（进程崩溃；正式评测中该用例失败） |
| 影响版本 | ≤ `83d1ae0d2` |
| 修复位置 | 工作区 `minivectordb/engine.py:44`（`convert()` 的 except 元组加入 `RecursionError`，归类为 `INVALID_VECTOR`） |
| 回归测试 | `tests/test_recovery.py:84` `test_deep_invalid_vector_does_not_abort_transaction` |

最小复现：

```sql
CREATE TABLE v (id BIGINT PRIMARY KEY, x VECTOR(2));
INSERT INTO v VALUES (1,'[0,[[[... 约 1200 层嵌套 ...]]]]');
```

- 实际：进程退出码 1；stdout 无任何 `ERROR` 响应；stderr 末行
  `Fatal storage/runtime error: maximum recursion depth exceeded while decoding a JSON array from a unicode string`
- 期望：返回 `INVALID_VECTOR` 类错误，进程继续服务下一条语句（公开题包约定“错误后继续执行”）。

根因：`json.loads` 对深嵌套数组抛出 `RecursionError`（`RuntimeError` 的子类）；`convert()` 的 `except (ValueError, OverflowError, TypeError, struct.error)` 未包含它，异常穿透到 `__main__.main` 的 `except (OSError, RuntimeError)`，被当作致命存储错误退出。

影响面：

- 任何约 1000 层以上嵌套的 JSON 向量字面量（畸形输入即可触发，不需要恶意构造）；
- 在显式事务内触发同样会终止进程，未提交事务随进程消失；
- 正式评测中一次崩溃即该用例失败，功能准入一票否决。

## 复现与验证记录

1. 干净副本：`git archive HEAD | tar -x -C <tmp>`，在副本内启动 `python -m minivectordb`：
   - DEF-1 返回 `ERROR TYPE_ERROR s exceeds VARCHAR(2)`；
   - DEF-2 进程退出码 1，stderr 为上述致命错误，stdout 无响应。
2. 工作区修复后：仓库套件 108 项全绿；独立组 P0–P7 全绿（含 P7 规格一致性 112 项，host + 容器）。
3. 两个回归测试分别覆盖：谓词等值/小于/删除/插入仍拒绝超长；事务内深嵌套 → 错误、事务仍可提交、重启保留。

## 建议

- 两个修复与对应测试随下一次提交入库；按官方要求与 `conversation.json` 同一次 commit。
- 可在独立组补两条黑盒检查（谓词超长、深嵌套向量），防止未来回退：
  - `SELECT ... WHERE s='<超长>'` 必须成功且返回空集；
  - 深嵌套向量插入必须返回错误且进程存活。
- 这两类输入属于“实现边界而非题目明写场景”，后续测试设计应继续从畸形输入与极端字面量入手，而不是只复述公开样例。

## 提交前复核（2026-11-10）

针对工作区最终源码重新执行：Windows 和 Linux 容器完整回归均为 108 项通过；Windows P7 为 112 项通过；Windows 与 Linux 容器 P8 均为 170 项通过、P9 均为 33 项通过。此前修复版镜像的独立检索 P5 为 19 项通过，AVX2 编译产物包含 default、avx2 和 resolver；第三轮进一步优化后的结果以本节的测试范围为准，不能把此前 P5 结果重标为第三轮结果。历史报告及学习材料的版本关联保留原提交。

这里的验证是本地测试，不代表官方协议预检或正式用例通过。源码和材料的提交状态以 Git 为准，上文“未提交”为撰写时的历史状态。
