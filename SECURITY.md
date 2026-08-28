# Security Policy

## Supported versions

本仓库当前按 release tag 提供可复现版本。安全修复优先针对最新 release 和当前 `main`。

## Reporting a vulnerability

请不要在公开 issue 中贴凭据、私有路径、受保护 fixture 或完整敏感日志。

报告应包含：

- 受影响版本或 commit；
- 可复现步骤；
- 影响范围；
- 是否涉及 approval receipt、authorization、verification command、source manifest、HTML escaping 或 path traversal；
- 最小必要日志或错误信息。

## Security boundaries

- `kym` / `tco` CLI 不调用 LLM。
- Verification command 必须来自项目配置中的 allowlist。
- Coverage JSON 不能注入 shell 命令。
- Approval receipt 绑定 artifact digest；KYM baseline 和 Target approval 还绑定 source closure digest。
- HTML renderer 必须 escape model/report 文本，不得执行用户输入。
- Skills 是 Agent 工作规程，不是授权面；它们不得自动创建 accepted state。

## Out of scope

- 对任意外部 Provider 输出语义正确性的保证。
- 对未安装 CLI 的环境提供 validate、verify 或 approve 执行能力。
- 对用户自行修改的 verification command allowlist 承担安全保证。

