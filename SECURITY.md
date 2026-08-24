# Security Policy

请不要在公开 issue 中提交凭据、私有路径、用户数据、受保护 fixture 或完整敏感日志。报告应包含受影响版本、最小复现步骤、影响范围和必要的非敏感日志。

安全边界：

- `kym` / `tco` CLI 不调用 LLM；
- verification command 必须来自显式 allowlist；
- candidate JSON 不应被解释为 approval 或 production authority；
- Skill 是 Agent 工作规程，不是授权面，也不会自动创建 accepted state。

对任意外部 Provider 输出的语义正确性、用户自行修改的命令 allowlist，以及未安装 CLI 的环境，本仓不作保证。
