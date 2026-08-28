# Contributing to kym-tco

本仓库的默认协作语言是中文；命令、schema 字段和 verdict 可保留英文，但必须解释证据边界。

## 开发环境

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -e .

kym --help
tco --help
```

## 本地验证

最小产品与治理检查：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m unittest discover -s tests/governance -v
python3 kb/tools/render_kb.py --check
python3 kb/tools/check_generated_docs.py
python3 tools/dashboard_lint.py --check
git diff --check
```

修改 Skill、references 或治理面时，还要运行相应 Skill/contract tests，并更新 `governance/upstream_manifest.json` 中的 digest（如适用）。

## 贡献边界

- 不提交凭据、私有路径、个人历史或不该公开的外部项目事实。
- 不把 passing tests、HTML render 或 Dashboard `done` 写成 approval。
- 不在候选 KYM/TCO 生成请求中自动创建 baseline、Target 或 Coverage receipt。
- 不把 `candidate_not_approved` 改写成 accepted state。
- `kb/data/` 是稳定 truth；`kb/docs/` 是确定性生成阅读面。
- `Dashboard/` 保存执行状态、blocker、decision、closeout evidence，不承载稳定合同。

## Pull request 建议

请在 PR 描述中写清：

1. 变更目标和非目标。
2. 修改的主要文件。
3. 运行过的验证命令。
4. 最大声明范围和已知边界。
5. 是否涉及 Skill、KB、Dashboard、schema、approval 或 release surfaces。

## 文档贡献

面向新用户的文档应优先解释：

- Skill 和 CLI 的区别；
- 哪些步骤是 LLM/Agent 辅助生成；
- 哪些步骤由 CLI/脚本确定性检查；
- 哪些步骤必须由人类或授权系统 approve；
- failure 时用户应该看哪个 artifact。

