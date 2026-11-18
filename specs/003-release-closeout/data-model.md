# 状态模型 · 发布收尾

## 候选提交

| 字段 | 含义 |
| --- | --- |
| `version` | 源码包版本，例如 `0.6.0` |
| `commit` | 候选提交哈希 |
| `date` | 记录日期 |
| `test_interpreter` | 实际运行测试的解释器和版本 |
| `test_command` | 实际执行的命令 |
| `tests_passed` | 通过数 |
| `ci_status` | `passed` / `failed` / `unverified` |
| `ci_evidence` | 运行链接或无法查询的原因 |

## 验收项

| 字段 | 含义 |
| --- | --- |
| `id` | T009、T010、I06 等 |
| `status` | `passed` / `blocked` / `known_limitation` / `unverified` |
| `evidence` | 已有证据的位置 |
| `blocker` | 当前阻塞原因；无则为空 |
| `unblock_condition` | 解除阻塞所需的条件 |
| `owner` | 需要提供条件的一方（项目所有者 / 外部试用者 / 维护者） |

### 状态转换

```text
unverified --证据不足--> blocked --条件满足并复测--> passed
passed --复测失败--> known_limitation
known_limitation --单独修复并复测--> passed
```

规则：

- 只有实际命令输出、CI 链接或真实任务报告可以把状态改为 `passed`。
- 模拟服务、脚本化驱动和固定判定必须注明与真实验证的差异。
- `blocked` 和 `known_limitation` 不得被路线图勾选为已完成。

## 路线图条目

| 字段 | 含义 |
| --- | --- |
| `stage` | 阶段一至阶段四或 v1.0 |
| `capability` | 能力描述 |
| `checked` | 是否勾选 |
| `evidence` | 代码、测试或验收记录位置；未勾选时写待验证项 |

### 勾选规则

- 有测试、真实任务或验收记录支撑 → 可勾选。
- 只有代码存在但无验证 → 保持未勾选，注明“已实现，待验收”。
- 明确不支持或未覆盖 → 保持未勾选，注明范围边界。
