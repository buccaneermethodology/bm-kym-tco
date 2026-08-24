# bm-kym-tco 安装与使用

## 安装 CLI

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
kym --help
tco --help
```

包名是 `bm-kym-tco`，import package 是 `kym_tco`。CLI 需要 Python 3.9+；运行时依赖由 `pyproject.toml` 声明。

## 安装两个 Codex Skills

将 `.codex/skills/build-kym` 和 `.codex/skills/build-tco-coverage` 复制到目标 Codex 工作区，或执行：

```bash
python tools/install_codex_skills.py --help
```

安装器需要显式目标目录；它不会安装或发现其他仓库 Skills。

## 使用顺序

1. 使用 `build-kym` 从明确来源起草候选 KYM。
2. 用 `kym validate`、`kym lint`、`kym render` 等命令做确定性检查。
3. 人类独立审查 KYM，再决定是否批准 baseline/Target。
4. 使用 `build-tco-coverage` 起草候选 TCO/Coverage。
5. 用 `tco validate-*`、`tco verify`、`tco render-preview` 做确定性检查。

CLI 不调用 LLM；Skill 也不自动创建 approval、promotion 或 production-ready 状态。
