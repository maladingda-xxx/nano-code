# 验证步骤 · 发布诊断与版本记录

## 前置条件

- 在仓库根目录，工作区干净
- 已安装可编辑包：`python -m pip install -e .\coding_agent`
- 使用与测试一致的 Python 3.11 环境

## 1. 导出诊断

```powershell
$env:API_KEY = 'sk-test-diagnostic-secret-1234567890'
nano-code doctor --export .\diag.json
```

预期：

- 返回码 `0`，输出导出文件路径
- `diag.json` 是有效 JSON
- 包含 `version`、`session_format`、`platform`、`session_db`、`checks`
- 不包含 `sk-test-diagnostic-secret-1234567890`

检查：

```powershell
Get-Content .\diag.json | ConvertFrom-Json | Select-Object version, session_format, api_key_configured
Select-String -Path .\diag.json -Pattern 'sk-test-diagnostic-secret'   # 应无输出
```

## 2. 路径错误

```powershell
nano-code doctor --export .
```

预期：返回非零，提示导出路径是目录，不生成诊断文件。

## 3. 运行回归测试

```powershell
.\.cache\verify-venv\Scripts\python.exe -m pytest coding_agent/tests -q
```

预期：现有 82 项和新增诊断测试全部通过。

## 4. 核对文档

在 `docs/terminal.md` 检查：

- `nano-code doctor --export` 用法
- 用户配置、项目设置、会话数据库和文件快照位置
- 备份、删除影响和清理步骤
- 数据格式版本 `1`

在 `CHANGELOG.md` 检查 0.6.0 条目和已知限制。

## 5. 清理验证文件

```powershell
Remove-Item .\diag.json
```

## 本轮验证记录

- **2026-11-18** 完成诊断导出、脱敏、数据格式版本和版本记录。
- **测试** `.\.cache\verify-venv\Scripts\python.exe -m pytest coding_agent/tests -q` → `86 passed`（原有 82 项 + 新增 4 项；两次运行 11.83–12.14s）。Python 3.11.4，pytest 9.2.0。
- **真实导出** 在 `coding_agent` 目录运行 `.\.cache\verify-venv\Scripts\python.exe -m nano_code --cwd . --export ..\.cache\diag-test.json doctor` → 退出码 `0`。文件包含 `version=0.6.0`、`session_format=1`、`api_key_configured=True`；`sk-test-diagnostic-secret-1234567890` 未出现在文件中。
- **路径错误** 导出到目录返回 `2`，错误信息包含“目录”，测试覆盖。
- **文档** `CHANGELOG.md`、`docs/terminal.md`、`README.md` 和 `plan.md` 已更新。
- **未运行** 未发起模型请求，未修改会话数据格式。
