# kym-tco 开源准备检查清单

本文记录本仓库面向开源分发时应满足的本地准备项。它不等于已经完成远端公开发布，也不等于 PyPI 发布。

## 已具备的基础面

- `LICENSE`：Apache License 2.0。
- `NOTICE`：记录治理材料来源和 adaptation 边界。
- `README.md` / `README.zh-CN.md`：项目介绍、CLI/Skill 边界、基础命令。
- `pyproject.toml` / `setup.py`：Python package metadata 与 `kym`、`tco` console scripts。
- `.gitignore`：排除 `.kym-tco/`、build outputs、cache。
- `tests/`：产品、Skill、治理和 BDD companion 检查。
- `tools/install_codex_skills.py`：选择性安装 Skills。

## 本轮补齐的开源面

- 安装与使用指南：`docs/INSTALLATION_AND_USAGE.zh-CN.md`。
- KYM 到 TCO 原理说明：`docs/KYM_TO_TCO_WORKFLOW.zh-CN.md`。
- 开源准备检查清单：本文件。
- 贡献指南：`CONTRIBUTING.md`。
- 安全策略：`SECURITY.md`。
- sdist 文件清单：`MANIFEST.in`。

## 发布前必须重新确认

### 1. 私有路径和凭据

运行：

```bash
rg -n "/Users/|BEGIN .*PRIVATE|AKIA|sk-|xoxb-|ghp_|gho_|password|secret|token" \
  README.md README.zh-CN.md docs CONTRIBUTING.md SECURITY.md pyproject.toml setup.py MANIFEST.in \
  --glob '!docs/OPEN_SOURCE_CHECKLIST.zh-CN.md'
```

确认没有凭据、真实私有路径或不该公开的个人信息。

排除本文件是为了避免扫描命令里的示例规则匹配到它自己。

### 2. License / Notice

确认：

- `LICENSE` 存在且内容完整；
- `NOTICE` 解释 adaptation 来源、边界和 review requirement；
- 若未来引入新第三方材料，必须更新 NOTICE 或相关 provenance。

### 3. Package metadata

运行：

```bash
python3 -m build
```

或在本地可用工具内完成 sdist/wheel 构建。确认包里包含必要文档和 Skill 文件。

### 4. 测试

推荐最小检查：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m unittest discover -s tests/governance -v
python3 kb/tools/render_kb.py --check
python3 kb/tools/check_generated_docs.py
python3 tools/dashboard_lint.py --check
git diff --check
```

如果修改了 Skill 或 references，还需要：

```bash
KYM_TCO_SKILL_ROOT=.codex/skills PYTHONPATH=src \
  python3 -m unittest tests.governance.test_skills tests.test_skill_contracts tests.governance.test_skill_behavior
```

### 5. 开源边界

公开前需要人类确认：

- 是否公开整个 repo，还是只公开 Skills 和 CLI 子集；
- 是否同步 GitHub Release；
- 是否发布 PyPI；
- 是否允许携带 `kb/BM-doc/` 参考材料；
- 是否需要移除或压缩 Dashboard 历史；
- 是否需要单独的 Code of Conduct 或 issue templates。

## 不应被误读的内容

- 开源准备完成不等于远端已经公开。
- GitHub Release 不等于 PyPI 发布。
- Skill 安装成功不等于 CLI 安装成功。
- CLI validation 通过不等于 KYM/TCO 语义批准。
- `tco verify` 通过不自动提升 Coverage state。
- HTML preview 是阅读界面，不是 approval receipt。
