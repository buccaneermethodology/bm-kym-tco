# kym-tco

[English README](README.md)

`kym-tco` 为两类 Agent 工作流提供确定性工具：

- `build-kym`：不可变来源快照、KYM 修订、Markdown 阅读视图、差异、baseline 批准和 Target 批准。
- `build-tco-coverage`：基于已批准 Target 的 TCO 模型、证据提案、白名单验证、Coverage 批准、Profile 对比以及 HTML/JSON 报告。

CLI 本身不会调用 LLM。调用它的 Agent 负责语义分析与综合；工具负责强制执行来源追溯、修订、批准、图结构、词汇和证据边界。

## 安装

```bash
uv tool install --editable .
kym --help
tco --help
```

支持 Python 3.9 及以上版本。项目数据保存在 `<workspace>/.kym-tco/<product-id>/`。

更完整的 Skills 与 CLI 安装说明见
[`docs/INSTALLATION_AND_USAGE.zh-CN.md`](docs/INSTALLATION_AND_USAGE.zh-CN.md)。
面向新用户的 KYM 到 TCO 原理说明见
[`docs/KYM_TO_TCO_WORKFLOW.zh-CN.md`](docs/KYM_TO_TCO_WORKFLOW.zh-CN.md)。
贡献与安全策略见 [`CONTRIBUTING.md`](CONTRIBUTING.md) 和
[`SECURITY.md`](SECURITY.md)。

## 推荐的 Codex 工作流

语义工作建议使用仓库内置的两个 Skill。新建 Codex 任务后，可使用下面的提示词；请把 `<conversation-or-document-path>` 替换为待分析输入文件的路径：

```text
$build-kym

请根据这段产品对话建立 KYM：
- 产品 ID：<product-id>
- 工作区：<workspace-path>
- 输入文件：<conversation-or-document-path>
- 先生成可审查的 KYM draft，不要自动批准 baseline 或 Target。

Agent 必须：
1. 保存不可变的输入快照。
2. 生成包含来源引用、陈述类型、时间状态和支持状态的 KYM JSON。
3. 渲染 Markdown 阅读视图。
4. 列出冲突、未决问题和来源边界。
5. 在“draft 已可供审查”处停止。
```

审查 draft 后，需要通过一条新的、明确的消息批准 baseline：

```text
批准当前 KYM revision 作为 baseline。
```

Target 必须单独审查，然后通过另一条明确消息批准：

```text
批准 Target-<target-id>。
```

只有 baseline 和 Target 都取得有效 receipt 后，才能开始 TCO 工作。新建或继续一个 Codex 任务并输入：

```text
$build-tco-coverage

使用已批准的 <product-id> KYM baseline 和 Target-<target-id>：
1. 建立稳定的 TCO 功能覆盖树。
2. 为每个 M 节点编写 TSP。
3. 创建 Coverage proposal。
4. 验证仓库证据。
5. 报告 Target profile 的 Met、Gap 和声明上限。
6. 不要自动批准 Coverage。
```

审查 proposal package 后，再明确批准：

```text
批准当前 TCO model 和 Coverage proposal。
```

Skill 负责语义 draft 和分解，但所有批准都必须与创建 draft 或 proposal 的请求分开进行。

## KYM 固定模板与 Golden 示例

KYM draft 使用固定的用户可读层级：

```text
Customer
├── Customers
├── Users
└── Stakeholders
Product
├── Info
├── Naming Conventions
├── Scope
├── Exclusions
├── Business Rules
├── Documents
├── Problems Unsolved
└── Standards
Project
├── Deliverables
├── Equipments and Tools
├── Schedule
└── Team
```

一级和二级节点的名称、顺序及空节点均必须保留。三级及以下内容可根据来源和分析组织。

角色归类遵循以下原则：

- Customers 通常是付费方、需求提出方或验收方，通常以组织为单位。
- Users 是直接使用功能的具体人员，也可以是通过内部接口调用功能的具体人员。
- Stakeholders 保存没有归入 Customers 或 Users 的组织级受益者。
- 同一角色只按最主要的关系归入 Customers、Users 或 Stakeholders 之一，避免重复。

机器所需的 provenance 保存在 JSON 和 source manifest 中；最终用户可读 Markdown 不包含 HTML validation 注释。来源中未明确的新解释不是必填内容；如果生成，必须使用 `support_status=inferred`，不能伪装成来源支持的事实。

## 直接使用 CLI

### 外部业务仓的安装版 Supported Host 流程

业务仓只需安装的 `kym`/`tco` CLI 与两个 Skills，不需要复制本仓库的 `src/kym_tco/`、内部 host registry 或工具源码。Skill 不会在任意宿主上自动触发验证流水线；候选 KYM/TCO/Coverage 包完成后，由操作者显式执行：

1. 准备 workspace-relative `source_input_manifest_v1`，构建并重验 ledger：

   ```bash
   tco --workspace "$PWD" source-ledger build \
     --input-manifest .kym-tco/<product-id>/source-inputs.json \
     --output .kym-tco/runtime/source-ledgers/<revision>.json
   tco --workspace "$PWD" source-ledger verify \
     --input-manifest .kym-tco/<product-id>/source-inputs.json \
     --ledger .kym-tco/runtime/source-ledgers/<revision>.json
   ```

   多份输入使用重复的 `--input-manifest`。ledger 保存原 manifest 与来源 bytes 的摘要、声明分母和 normalization rule version；`external-run` 会再次从原 manifest 重算。
2. 查看宿主真实选择的 project/global Skill。项目副本存在时优先；若坏损或版本冲突，修复前不得改用 global。宿主不能提供可验证 resolver attestation 时，操作者保存本次选择的简短证据说明，然后记录 operator assertion：

   ```bash
   tco --workspace "$PWD" selection-record create \
     --build-kym-skill-dir <build-kym目录> --build-kym-skill-source <project|global> --build-kym-reason '<选择理由>' \
     --build-tco-coverage-skill-dir <build-tco-coverage目录> --build-tco-coverage-skill-source <project|global> --build-tco-coverage-reason '<选择理由>' \
     --operator-id <操作者ID> --evidence-ref .kym-tco/runtime/skill-selection/selection-review.md \
     --output .kym-tco/runtime/skill-selection/current.json
   ```

   项目副本存在时对相应 skill 增加 `--project-copy-exists <skill-id>`。`operator_confirmed` 是操作者 assertion，不是 CLI 声称看到了宿主界面。
3. 对两个实际目录分别运行 `tco host-preflight --host-profile installed_cli_external_workspace_v1 --skill-id <skill-id> --skill-dir <实际目录> --skill-source <project|global>`。该命令只预检 runtime/profile/Skill identity，不运行候选 gates。
4. 两个 Skill candidate package 都完成后运行：

   ```bash
   tco --workspace "$PWD" external-run \
     --host-profile installed_cli_external_workspace_v1 \
     --build-kym-skill-dir <build-kym目录> --build-kym-skill-source <project|global> \
     --build-tco-coverage-skill-dir <build-tco-coverage目录> --build-tco-coverage-skill-source <project|global> \
     --source-manifest .kym-tco/<product-id>/source-inputs.json \
     --source-ledger .kym-tco/runtime/source-ledgers/<revision>.json \
     --skill-selection-record .kym-tco/runtime/skill-selection/<selection-id>.json \
     --output <KYM候选文件> --output <TCO候选文件> \
     --attempt-id <唯一attempt-id>
   ```

   CLI 返回 `bundle_ref`，它是确定性验证的 **validation evidence bundle**；两个 Skill 生成的 **candidate package** 仍是候选内容权威，CLI 不改写它们。受支持消费者对每个候选及 evidence 执行 `tco --workspace "$PWD" external-consume-check --candidate <候选文件> --evidence <bundle_ref>`。`consumable=true` 仍只表示 `candidate_not_approved`。failed/blocked/unknown/retained-partial 不能消费；检查 `.kym-tco/runtime/host-failure-records/` 和 bundle disposition，修复输入后用新 attempt ID 重试。

该流程不代表语义正确、Coverage Met、approval/receipt、promotion 或公开发行；缺少工具时只能使用 Skill 内的 CLI missing 诊断，不得声称宿主流水线已运行。

在目标项目仓库中初始化 KYM：

```bash
cd /path/to/your/project

kym --workspace . init \
  --product-id <product-id> \
  --coverage-mode repo

kym --workspace . ingest \
  --product-id <product-id> \
  --source /path/to/conversation.txt
```

项目数据保存位置：

```text
<workspace>/.kym-tco/<product-id>/
```

查看全部命令：

```bash
kym --help
tco --help
```

CLI 负责确定性的输入摄取、验证、差异比较、receipt、verification 和渲染。CLI 不理解产品对话，也不会自行生成 KYM 语义内容；语义 draft 应使用前述两个 Skill。

## 固定版本的 Skill 分发

团队协作或要求结果可复现时，应选择一个明确的 release tag，并使用该 tag
内提交的 Skills。不要在项目之间复制未版本化的全局 Skill 目录。

```bash
git clone --branch <release-tag> --depth 1 https://github.com/buccaneermethodology/bm-kym-tco.git
cd kym-tco

python3 tools/install_codex_skills.py \
  --source .codex/skills \
  --target /path/to/target-project/.codex/skills \
  --dry-run

# 只安装两个生成 Skill。
python3 tools/install_codex_skills.py \
  --source .codex/skills \
  --target /path/to/target-project/.codex/skills \
  --skill build-kym \
  --skill build-tco-coverage \
  --dry-run

python3 tools/install_codex_skills.py \
  --source .codex/skills \
  --target /path/to/target-project/.codex/skills \
  --skill build-kym \
  --skill build-tco-coverage \
  --install
```

默认操作是 dry-run。安装只创建缺失的 Skill，逐文件验证复制结果，并拒绝覆盖
内容不同的目标 Skill。重复传入 `--skill NAME` 可选择性安装；省略该参数时安装全部
repo-local Skill。所选名称必须存在且不能重复。升级必须显式选择新 tag，再重新执行
dry-run 和安装；Skill 不会自动更新。release tag、测试通过或 installer report 都不能创建来源/许可证
authority，也不能创建任何 KYM/TCO approval。

## 只读候选生成审计

完成正常 validation 后，可在请求人类审查前运行只读 semantic risk audit：

```bash
python3 tools/audit_generation_quality.py kym \
  --workspace <workspace> \
  --product-id <product-id> \
  --candidate <kym.json> \
  [--base <base-kym.json>]

python3 tools/audit_generation_quality.py tco \
  --workspace <workspace> \
  --product-id <product-id> \
  --model <tco.json> \
  [--coverage <coverage.json>] \
  [--base-model <base-tco.json>]
```

该工具只向 stdout 输出稳定 JSON，不写 workspace artifact 或 receipt。发现 audit error 时退出 `1`，表示候选尚未达到可审查状态；只有 warning 时退出 `0`，但仍需人工判断；合同或 I/O 错误退出 `2`。报告不包含 approval verdict。Schema validation、零 finding、Golden 相似或 verification 通过都不能自动批准 KYM baseline、Target、TCO model 或 Coverage proposal。

安装后的两个 Skill 各自携带调用同一 API 的 bundled wrapper，因此不要求目标项目也包含本仓 `tools/` 目录。

## Authority 链

```text
来源快照
-> KYM draft
-> 人类或已绑定授权系统的 KYM baseline receipt
-> 人类或已绑定授权系统的 Target receipt
-> TCO model draft
-> Coverage proposal
-> 白名单 verification
-> 人类或已绑定授权系统的 Coverage receipt
-> TCO Coverage report
```

Markdown 视图、命令通过、模型文件或其中的状态文字都不能自动创建批准。若批准来自系统，receipt 必须写 `authorized_system`，并绑定预先存在的 workspace 授权；不得伪造为 `human`。

## KYM 命令

```bash
kym init --product-id <id> [--coverage-mode repo|not_evaluated]
kym ingest --product-id <id> --source <conversation-or-document>
kym inspect-legacy --input <legacy-kym.md>
kym validate --product-id <id> --input <draft.json>
kym store-initial --product-id <id> --candidate <draft.json>
kym render --product-id <id> --input <revision.json>
kym diff --product-id <id> --base <revision.json> --candidate <candidate.json>
kym merge --product-id <id> --base <revision.json> --candidate <candidate.json>
kym approve baseline --product-id <id> --revision <revision.json> --approval-ref <ref> [--approval-source human|authorized_system --approved-by <主体> --authorization <workspace>/.kym-tco/<id>/automation/authorizations/<authorization>.json]
kym approve target --product-id <id> --revision <revision.json> --target-id <id> --approval-ref <ref> [--approval-source human|authorized_system --approved-by <主体> --authorization <workspace>/.kym-tco/<id>/automation/authorizations/<authorization>.json]
```

每条获批陈述都具有稳定 identity、类型、时间/支持状态、authority 及来源/轮次/行号 provenance。Baseline 和 Target receipt 同时绑定已审查 artifact 与 source-closure digest；该 digest 覆盖 manifest identity/content、snapshot digest、chunk ID/reference/digest 和轮次元数据。

旧 revision 永远不会被覆盖。冲突和 unresolved 陈述可以保留在已批准 baseline 中，但不能成为已批准 Target requirement。

## TCO 命令

```bash
tco init --product-id <id> [--coverage-mode repo|not_evaluated]
tco validate-model --product-id <id> --model <model.json>
tco validate-coverage --product-id <id> --model <model.json> --coverage <proposal.json>
tco verify --product-id <id> [--node <node-id>] --output <results.json>
tco approve-coverage --product-id <id> --model <model.json> --proposal <proposal.json> --approval-ref <ref> --approved-by <主体> [--approval-source human|authorized_system --authorization <workspace>/.kym-tco/<id>/automation/authorizations/<authorization>.json]
tco render --product-id <id> --model <model.json> --coverage <approved-coverage.json>
tco compare-profiles --product-id <id> --model <model.json> --coverage <approved-coverage.json>
tco diff --base <json> --candidate <json>
```

保留的 Coverage 状态为：

```text
unknown < not_started < design_only < schema_validator < test_bound < bounded_runtime < integrated_runtime
```

项目可以增加状态或修改显示名称，但不能改变保留 ID、rank 或定义。Verification 通过只会刷新结果，绝不会自动修改 Coverage 状态。在 `not_evaluated` 模式中，Profile 显示 `N/A`，而不是 `0/N`。

## Verification 与自动化边界

Verification 命令以 `argv`、受限 cwd、timeout、node ID 和允许的环境变量名注册在项目配置中。`argv[0]` 必须是可执行文件的绝对路径；子进程只接收 `allowed_env` 明确列出的环境变量，不会隐式继承宿主机 `PATH`。Coverage JSON 不能注入 shell 命令。

v1 不包含定时自动化。自动化可重新运行 `tco verify`、`tco render` 并输出变化报告；遇到语义模型、Target、Coverage proposal、authorization 或 governance checkout 漂移时必须停止并 rebaseline。只有同时提供 `--approval-source authorized_system`、系统主体和 workspace 内预先存在的授权文件，且该授权绑定 Goal、产品、scope、Target、有效期与 approval reference 前缀时，才可创建系统 receipt；CLI 不创建该授权文件，系统 receipt 绝不标为 `human`。
