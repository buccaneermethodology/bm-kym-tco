# bm-kym-tco

面向可追溯 KYM 草稿和候选 TCO/Coverage 工作的确定性 CLI 与两个可移植 Codex Skill。

首个公开版本只包含：

- `kym`、`tco` CLI 及其必要运行时/schema 闭包；
- `.codex/skills/build-kym`；
- `.codex/skills/build-tco-coverage`；
- 公开文档、许可证、CI 和选择性 Skill 安装器。

CLI 不调用 LLM。Skill 负责指导语义起草，CLI 负责确定性校验、打包、比较和渲染。草稿、预览或通过检查都不等于语义批准、promotion 或生产就绪。

## 安装

```bash
python -m pip install .
kym --help
tco --help
```

支持 Python 3.9 及以上。包名为 `bm-kym-tco`，import package 保持为 `kym_tco`，命令为 `kym` 与 `tco`。

## 两个 Skill

将 `.codex/skills/` 下的两个目录复制到 Codex 工作区，或使用 `tools/install_codex_skills.py` 指定目标目录。公开仓不包含、也不暗示其他仓库 Skills。

`build-kym` 用于生成带来源追踪的 KYM 草稿；`build-tco-coverage` 用于生成候选 TCO 模型、Coverage proposal 和预览。人类审查与批准始终是独立步骤。

## 范围与维护

`bm-kym-tco` 是内部 `kym-tco` 的单向公开投影；内部仓库仍是维护真源。公开变更必须经过显式 allowlist、许可证/泄漏审查和 digest read-back；公开仓不会自动反向合并到内部仓库。

详见[安装指南](docs/INSTALLATION_AND_USAGE.zh-CN.md)、[KYM 到 TCO 工作流](docs/KYM_TO_TCO_WORKFLOW.zh-CN.md)、[发布检查清单](docs/OPEN_SOURCE_CHECKLIST.zh-CN.md)、[贡献指南](CONTRIBUTING.md)和[安全策略](SECURITY.md)。
