# 状态模型 · 发布诊断

## 诊断记录

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `version` | string | Nano Code 源码包版本 |
| `session_format` | integer | 会话数据库和文件快照的数据格式版本 |
| `python` | string | 运行解释器版本 |
| `platform` | string | 操作系统和版本 |
| `executable` | string | 解释器路径 |
| `workspace` | string | 工作区根目录 |
| `config_home` | string | 用户配置目录 |
| `session_db` | string | 会话数据库绝对路径 |
| `provider` / `model` | string | 当前服务商和模型 |
| `effort` | string | 推理强度或 `auto` |
| `permission_mode` | string | 当前权限模式 |
| `api_key_configured` | boolean | 是否已配置 Key；不包含 Key 值 |
| `project_extensions_trusted` | boolean | 项目扩展是否已信任 |
| `skills` | integer | 发现的自定义 Skill 数量 |
| `mcp_configured` | list | 已配置的 MCP 服务名 |
| `extension_errors` | list | 扩展加载错误，导出时脱敏 |
| `checks` | object | Python 版本和工作区检查结果 |

## 脱敏规则

| 输入 | 输出 |
| --- | --- |
| 字段名匹配敏感词且值为字符串 | `[REDACTED]` |
| 字符串包含 `sk-`、`gho_`、`github_pat_` 或 `Bearer <token>` | 令牌部分替换为 `[REDACTED]` |
| 布尔字段 `api_key_configured` | 原样保留 |
| 数字、路径、版本号 | 原样保留 |

## 数据格式版本

| 版本 | 含义 |
| --- | --- |
| `1` | 当前会话数据库布局；兼容 v0.3 存档的会话表和文件快照表 |

状态转换：本轮不迁移，版本号固定为 `1`。未来格式变更时递增，并提供备份和迁移说明。

## 存储位置

| 位置 | 内容 | 清理影响 |
| --- | --- | --- |
| `~/.nano-code/settings.json` | 用户设置 | 删除后恢复默认模型配置 |
| `~/.nano-code/.env` | 本机凭据 | 删除后需要重新提供 API Key |
| `<项目>/.nano/settings.json` | 受信任项目设置 | 删除后项目覆盖失效 |
| `<项目>/.coding_agent_sessions.db` | 对话、工具输出、文件快照、检查点 | 删除后无法恢复会话、检查点和撤销记录；工作区文件不受影响 |
