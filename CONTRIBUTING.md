# Contributing to kym-tco

Contributions should keep user-facing documentation clear and preserve the distinction between candidate output, deterministic validation, and human approval.

## 开发环境

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -e .

kym --help
tco --help
```

## Validation

最小产品与治理检查：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m unittest discover -s tests/governance -v
git diff --check
```

For changes to a Skill or its references, run the matching Skill contract checks and verify the distribution digests.

## 贡献边界

- 不提交凭据、私有路径、个人历史或无关项目事实。
- 不把测试通过或 HTML 渲染当作批准。
- 不在候选 KYM/TCO 生成请求中自动创建 baseline、Target 或 Coverage receipt。
- 不把 `candidate_not_approved` 改写成 accepted state。

## Pull request 建议

请在 PR 描述中写清：

1. 变更目标和非目标。
2. 修改的主要文件。
3. 运行过的验证命令。
4. 最大声明范围和已知边界。
5. 是否影响 Skills、schema、批准边界或发行文件。

## 文档贡献

面向新用户的文档应优先解释：

- Skill 和 CLI 的区别；
- 哪些步骤是 LLM/Agent 辅助生成；
- 哪些步骤由 CLI/脚本确定性检查；
- 哪些步骤必须由人类或授权系统 approve；
- failure 时用户应该看哪个 artifact。

