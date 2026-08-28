# KYM/TCO Skills 与 CLI 安装使用指南

本文面向第一次使用 `kym-tco` 的用户。核心结论是：`build-kym` / `build-tco-coverage` Skills 和 `kym` / `tco` CLI 是两层东西，二者都准备好，才能获得完整体验。

## 一句话总览

```text
安装 Skills
  让 Codex Agent 知道如何做 KYM/TCO 语义生成

安装 kym-tco CLI
  让本机或 Agent 能运行 validate、lint、verify、render、approve 等确定性命令

只安装 Skills
  可以指导 Agent 起草候选内容，但不能真正执行 CLI 验证或批准命令
```

## 前置条件

- Python 3.9 或更高版本。
- 能在目标环境运行 shell 命令。
- 如果要让 Codex Agent 自动跑验证，Agent 所在环境必须能调用 `kym` 和 `tco`。
- 如果要安装 Codex Skills，目标项目应有 `.codex/skills/` 目录；没有也可以由安装脚本创建。

推荐先确认：

```bash
python3 --version
git --version
```

## 安装 CLI

### 从本地 checkout 安装

适合开发或本机验证：

```bash
git clone https://github.com/buccaneermethodology/kym-tco.git
cd kym-tco

uv tool install --editable .

kym --help
tco --help
```

如果不用 `uv`，也可以在虚拟环境里用 pip editable 安装：

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -e .

kym --help
tco --help
```

### 从固定 tag 安装

团队协作建议使用明确 tag，避免每个人拿到不同版本：

```bash
python3 -m pip install \
  "git+https://github.com/buccaneermethodology/kym-tco.git@v0.3.8"

kym --help
tco --help
```

如果你需要复现某个历史结果，请使用当时记录的 tag，而不是默认分支。

## 安装两个 Skills

两个生成 Skill 是：

- `build-kym`
- `build-tco-coverage`

安装到某个目标项目：

```bash
cd /path/to/kym-tco

python3 tools/install_codex_skills.py \
  --source .codex/skills \
  --target /path/to/target-project/.codex/skills \
  --skill build-kym \
  --skill build-tco-coverage \
  --dry-run
```

确认 dry-run 输出符合预期后，再执行：

```bash
python3 tools/install_codex_skills.py \
  --source .codex/skills \
  --target /path/to/target-project/.codex/skills \
  --skill build-kym \
  --skill build-tco-coverage \
  --install
```

安装脚本默认是 dry-run。它会拒绝覆盖内容不同的已有 Skill，避免静默破坏目标项目。

## 验证安装是否完整

在目标项目中检查：

```bash
kym --help
tco --help
test -f .codex/skills/build-kym/SKILL.md
test -f .codex/skills/build-tco-coverage/SKILL.md
```

解释：

- `kym --help` 和 `tco --help` 通过，表示 CLI 可用。
- 两个 `SKILL.md` 存在，表示 Agent 能读取生成工作流。

## 推荐的一次性使用方式

在 Codex 中对目标项目发起请求：

```text
请使用 build-kym 和 build-tco-coverage，对当前 repo 生成 KYM 和 candidate-only TCO。

要求：
1. 先检查 kym/tco CLI 是否可用。
2. 自动运行 KYM validate、lint、audit、render。
3. 自动运行 TCO validate-candidate-model、validate-candidate-coverage、verify、audit、compare-candidate-profiles、render-preview。
4. 不自动 approve。
5. 最终给我 KYM reader、TCO preview、verification results 和 audit report 的路径。
```

当前 Skill 会指导 Agent 运行这些步骤；未来的最佳体验 Loop Goal 会把这些步骤进一步收敛成一条高层命令或固定自动流水线。

## 手动运行 KYM 基础流程

```bash
WORKSPACE=/path/to/target-project
PRODUCT_ID=my-product
SOURCE=/path/to/source-conversation-or-doc.md

kym --workspace "$WORKSPACE" init \
  --product-id "$PRODUCT_ID" \
  --coverage-mode repo

kym --workspace "$WORKSPACE" ingest \
  --product-id "$PRODUCT_ID" \
  --source "$SOURCE"
```

注意：CLI 只负责摄取和验证，不会自己理解对话并生成 KYM JSON。KYM JSON 的语义起草由 Agent 使用 `build-kym` 完成。

生成候选 KYM JSON 后运行：

```bash
kym --workspace "$WORKSPACE" validate \
  --product-id "$PRODUCT_ID" \
  --input /path/to/kym-candidate.json

kym --workspace "$WORKSPACE" lint \
  --product-id "$PRODUCT_ID" \
  --input /path/to/kym-candidate.json \
  --strict

kym --workspace "$WORKSPACE" store-initial \
  --product-id "$PRODUCT_ID" \
  --candidate /path/to/kym-candidate.json

kym --workspace "$WORKSPACE" render \
  --product-id "$PRODUCT_ID" \
  --input /path/to/stored-kym-revision.json
```

## 手动运行 TCO candidate preview 流程

```bash
WORKSPACE=/path/to/target-project
PRODUCT_ID=my-product
MODEL=/path/to/tco-model.json
COVERAGE=/path/to/coverage-proposal.json
OUT=/path/to/tco-output

tco --workspace "$WORKSPACE" validate-candidate-model \
  --product-id "$PRODUCT_ID" \
  --model "$MODEL"

tco --workspace "$WORKSPACE" validate-candidate-coverage \
  --product-id "$PRODUCT_ID" \
  --model "$MODEL" \
  --coverage "$COVERAGE"

tco --workspace "$WORKSPACE" verify \
  --product-id "$PRODUCT_ID" \
  --output "$OUT/verification-results.json"

tco --workspace "$WORKSPACE" compare-candidate-profiles \
  --product-id "$PRODUCT_ID" \
  --model "$MODEL" \
  --coverage "$COVERAGE"

tco --workspace "$WORKSPACE" render-preview \
  --product-id "$PRODUCT_ID" \
  --model "$MODEL" \
  --coverage "$COVERAGE" \
  --verification-results "$OUT/verification-results.json" \
  --output-dir "$OUT"
```

`render-preview` 生成的是 candidate preview。它不创建 approval receipt，也不把 Coverage proposal 变成 approved Coverage。

## Approve 命令什么时候用

Approve 是人类或授权系统的明确决定，不应该由 Skill 在生成候选时自动执行。

KYM baseline approve：

```bash
kym --workspace "$WORKSPACE" approve baseline \
  --product-id "$PRODUCT_ID" \
  --revision /path/to/kym-revision.json \
  --approval-ref "review://your-human-review" \
  --approved-by "your-name"
```

KYM Target approve：

```bash
kym --workspace "$WORKSPACE" approve target \
  --product-id "$PRODUCT_ID" \
  --revision /path/to/kym-revision.json \
  --target-id target-a \
  --approval-ref "review://your-human-review" \
  --approved-by "your-name"
```

TCO Coverage approve：

```bash
tco --workspace "$WORKSPACE" approve-coverage \
  --product-id "$PRODUCT_ID" \
  --model "$MODEL" \
  --proposal "$COVERAGE" \
  --approval-ref "review://your-human-review" \
  --approved-by "your-name"
```

如果 CLI 不可用，这些 approve 命令不能执行，也不会落地 receipt。

## 常见问题

### 只安装 Skills，可以 approve 吗？

不可以。Approve 是 CLI 命令，需要 `kym` 或 `tco` 可执行文件存在。

### 只安装 CLI，可以生成 KYM/TCO 吗？

只能做确定性摄取、验证、渲染和批准。CLI 不会调用 LLM，也不会自己完成语义起草。语义生成需要 Agent 使用 Skill。

### verification 通过会自动提升 Coverage state 吗？

不会。`tco verify` 只刷新 verification results。若要让 Coverage proposal 反映新证据，需要重新生成或修订 proposal；即便如此仍然是 candidate，直到人类或授权系统 approve。

### HTML 是最终权威吗？

不是。HTML 是阅读界面。权威数据仍在 JSON、manifest、verification results、approval receipt 和 source manifests 中。
