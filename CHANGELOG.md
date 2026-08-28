# Changelog

## [0.4.0] - 2026-08-16

- 增加受控多输入 TCO candidate 编排、逐 requirement 自验证、负向结果、package、preview 与 execution witness。
- 保持 `candidate_not_approved` 边界；本版本不创建 semantic approval、approval receipt 或 production readiness 声明。

## [0.3.10] - 2026-08-15

### Changed

- 固定 SP-029 三个 Agent Synthesis M1 语义验证策略实现后的当前 release tree：KYM/TCO/Coverage reviewer、semantic regression、breadth aggregation、三个 M1 eligibility policy 与三个 Target Profile 可逆分区保持 observation-only。
- 将 `build-kym` 与 `build-tco-coverage` 的 doctor 期望 CLI 版本同步为 `0.3.10`。

### Distribution

- 发布固定版本 `v0.3.10`；用于同步 kym-tco repo-local、semx-cli repo-local 与本机全局的两个指定 Skill。

### Boundary

- 本条目不代表 PyPI、KYM/TCO/Coverage approval、promotion、semantic correctness、production readiness、runtime result 或任意宿主恒真。

## [0.3.9] - 2026-08-12

### Changed

- KYM reader 不再在每条 statement 旁重复 `source_id` 与 line range；这些机器 provenance 继续作为 KYM JSON 必填字段并由 validator fail closed，reader source inventory 继续显示 manifest path、source file、digests、repo revision/dirty 和 snapshot kind。
- self candidate 的输入边界从 repo `src` 扩大到 `src` 加 repo-local `build-kym`、`build-tco-coverage`，用于分别建模 deterministic CLI 与 Agent 语义生成/编排能力。
- 保留 TCO v4 execution/output/witness 对齐、reader-language runtime gate、candidate-only 与 no approval/receipt 边界。

### Distribution

- 发布固定版本 `v0.3.9`；仅同步 `build-kym`、`build-tco-coverage` 到 kym-tco repo-local、semx-cli repo-local与本机全局目标。

### Boundary

- 本条目不代表 PyPI、semx-cli 产品发布、任意 Agent 宿主恒真、KYM/TCO/Coverage 语义批准、P06 authority、receipt 或 production readiness。

## [0.3.8] - 2026-08-10

### Changed

- 固定 BI-009 / SP-027 的一次调用候选包、证据中心与 Goal 制定质量治理改动，保持 candidate-only、无 approval/receipt 边界。
- 将 `build-kym` 与 `build-tco-coverage` 的 doctor 期望 CLI 版本同步为 `0.3.8`。

### Distribution

- 发布固定版本 `v0.3.8`；用于同步 kym-tco repo-local、semx-cli repo-local 与本机全局的两个指定 Skill。

### Boundary

- 本条目不代表 PyPI、外部 IAM、无边界 Runtime、Provider 语义、Target/Coverage/receipt、production readiness 或任何 approval。

## [0.3.7] - 2026-08-09

### Changed

- 固定 SP-025 后的 TCO HTML Profile rows 列表泄漏修复、六个 M1 bounded runtime gate 与 self-repo KYM/TCO 生成链验证证据。
- 新增 `src/kym_tco/runtime_gates.py` runtime evidence 支撑，并保留 candidate-only、no approval/receipt 与 Coverage proposal 边界。

### Distribution

- 发布固定版本 `v0.3.7`；用于同步 repo-local、semx-cli repo-local 与本机全局 `build-kym`、`build-tco-coverage` Skill。两个 Skill 目录如无内容差异，则以 read-back `unchanged` 作为同步证据。

### Boundary

- 本条目不代表 PyPI、外部 IAM、无边界 Runtime、Provider 语义、Target/Coverage/receipt、production readiness 或任何 approval。

## [0.3.6] - 2026-08-04

### Changed

- TCO candidate HTML preview 顶部新增统一状态解释，明确 Coverage state ladder、effective state、claim state、lifecycle state、validation posture、resolution status 与 requirement row completeness 的边界。
- v4 preview 的 requirement completeness 可见文案改为 `Requirement row` / `Coverage 跟踪行` / `Rows present / total` 口径，避免把 `8/8` 误读为 Coverage 已满足、实现完成或验证通过。

### Distribution

- 发布固定版本 `v0.3.6`；用于同步 repo-local、semx-cli repo-local 与本机全局 `build-kym`、`build-tco-coverage` Skill。两个 Skill 目录本轮无功能变更时 read-back 可为 `unchanged`。

### Boundary

- 本条目不代表 PyPI、外部 IAM、Provider 语义、任意 semx-cli 历史 TCO candidate rewrite、Target/Coverage/receipt 或任何 approval。

## [0.3.5] - 2026-08-04

### Changed

- `build-tco-coverage` 增加 tco-r004 problem-driven improvement 修正：拆分 terminal decision、continuation、execution-terminal 与 M1 completion 输出语义；收紧 Target requirement evidence mapping、failure taxonomy、candidate readiness、lane authority、claim exclusion、Provider configuration、MC lifecycle grouping 与 lifecycle/validation/Coverage/breadth/resolution 正交轴 guidance。
- TCO v4 schema、validator、migration、candidate audit、profile comparison 与 HTML preview 同步上述边界，继续保持 `candidate_not_approved` 与不批准 Target/Coverage/Provider capability 的 claim ceiling。

### Distribution

- 发布固定版本 `v0.3.5`；用于同步 repo-local 与本机全局 `build-kym`、`build-tco-coverage` Skill。`build-kym` 本次无功能变更。

### Boundary

- 本条目不代表 PyPI、外部 IAM、Provider 语义、任意 semx-cli 历史 tco-r004 candidate rewrite、Target/Coverage/receipt 或任何 approval。

## [0.3.4] - 2026-08-03

- 修复自包含 CI：Skill contract tests 明确读取 checkout 内 repo-local Skills，不依赖运行器的全局 Codex 目录。

## [0.3.3] - 2026-08-03

- 完整展示 TCO M 节点结构化详情，加入项目绑定终态词表、Scope 完整性合同与自包含 CI。
- 清理 Dashboard、KB 与 manifest 的历史同步债务；保持 candidate-only、approval 与外部兼容边界。

## [Unreleased]

## [0.3.2] - 2026-07-30

### Changed

- `build-kym` 新增首稿语义放置门禁，明确区分产品行为、roadmap、taxonomy、protocol、Target 与 lane 概念，避免将非产品实现内容写入 Business Rules。
- `build-tco-coverage` 与 TCO v2 支撑合同新增 mandatory Scope、effective/claim 覆盖、breadth identity、Target-gap M1 的 fail-closed candidate materialization，以及对应报告与 HTML preview 投影。

### Distribution

- 发布固定版本 `v0.3.2`；仅发布 repo-local `build-kym` 与 `build-tco-coverage`，不执行本机全局 Skill 安装。

### Boundary

- 本条目不代表 PyPI、本机全局 Skill 安装、Provider 语义、任意 repo 业务语义、Target/Coverage/receipt 或任何 approval。

## [0.3.1] - 2026-07-29

### Changed

- `build-kym` 新增 reader-quality pass：对 Goal/计划输入区分 source snapshot、verified current fact 与 candidate/proposal，并收紧 Target Profile 的显式触发条件。
- 固化 Product/Project 节点归位、source-silent 留空及 effect status 与 final verdict 分离规则。

### Distribution

- 发布固定版本 `v0.3.1`；仅同步 repo-local 与本机全局 `build-kym` Skill。

### Boundary

- 本条目不代表任意 KYM 输出语义正确、PyPI、Provider 语义、SP-064 实际执行或任何 KYM/Target/TCO/Coverage approval。

## [0.3.0] - 2026-07-29

### Changed

- 发布 `0.3.0`：增加 `source_manifest.v2`、受配置根限制的 `kym ingest-repo`、candidate-only TCO v2、v1 迁移与 v2 Skills/KB。
- 普通 TCO CLI 入口拒绝 v1；迁移永远输出 `candidate_not_approved` 且保留 unresolved semantics，不复制 approval receipt。

### Distribution

- 发布固定版本 `v0.3.0`；同步 repo-local 与本机全局 `build-kym`、`build-tco-coverage` 两个 Skill，并保留旧全局 Skill 备份。

### Boundary

- 本条目不代表 PyPI、外部 IAM、Provider 语义、SP-064 实际执行或任何 KYM/Target/TCO/Coverage approval。

## [0.2.2] - 2026-07-23

### Fixed

- 将 S-020 Automation executable 修复固定为 release：当前 checkout 的 `tco` 可执行入口与 API 已同步到 candidate-only TCO preview 能力。
- `tco --help` 可见 `validate-candidate-model`、`validate-candidate-coverage`、`render-preview` 与 `compare-candidate-profiles`。
- candidate preview renderer/API 保留 `candidate_not_approved` 边界，不读取或创建 baseline、Target 或 Coverage receipt。

### Distribution

- 发布固定版本 `v0.2.2`；同步 8 个 repo-local kym-tco Skills，并对本机全局 Skill 目标执行 read-back。若目标内容已一致，安装报告保持 `unchanged`。

## [0.2.1] - 2026-07-23

### Added

- 新增 TCO candidate-only CLI/API：`validate-candidate-model`、`validate-candidate-coverage`、`compare-candidate-profiles` 与 `render-preview`。
- `render-preview` 输出 `tco-preview.html`、candidate report 与 manifest，首屏固定标记 `candidate_not_approved`。
- 新增 repo-local governed task entry：`task_entry.py assess/bootstrap/verify`、`governed-task-entry` KB、BDD readable cards 与八个 Skill 的默认入口桥接。

### Safety

- Candidate TCO 链路不读取 baseline、Target 或 Coverage receipt；既有 approved `validate-model`、`render`、`compare-profiles` 仍 fail-closed。
- 默认八个 repo-local Skill 在 non-trivial 持久写入前要求先进入可验证 Session/Design/AC/OPCM/Agent Log 路径；该 repo-local 合同不声称外部平台无条件自动调用。

### Distribution

- 发布固定版本 `v0.2.1`；同步 8 个 repo-local kym-tco Skills，并对本机全局 Skill 目标执行 read-back。若目标内容已一致，安装报告保持 `unchanged`。

## [0.2.0] - 2026-07-23

### Changed

- 将 SP-011 Automation Goal 设计收窄为无批准 `candidate-only` 链路：自动化只生成 KYM/TCO candidate、proposal、verification 与 HTML preview，不执行 baseline、Target 或 Coverage approval。
- P05 Target-B Automation 输入包改为 `candidate_not_approved`，显式列出 forbidden approval actions，并移除 authorization/approval 输入字段。
- Human-AI governance KB 与 Dashboard 投影同步区分“无批准 Automation Goal”和另一个显式 `authorized_system` receipt 流程。

### Safety

- Candidate HTML / Coverage proposal 必须标记为 preview，不得被解读为 approved report；当前产品 renderer 若只接受 approved coverage，Automation 必须暂停或使用 candidate preview，不能补造 receipt。

### Distribution

- 发布固定版本 `v0.2.0`；只更新 `build-kym` 与 `build-tco-coverage` 两个明确选择的 repo-local Skill 目标和本机全局 Skill 目标。若目标内容已一致，安装报告保持 `unchanged`。

## [0.1.5] - 2026-07-22

### Added

- 为 `authorized_system` approval source 增加受控授权文件 schema 与确定性校验：授权必须在产品 workspace 内预先存在，并绑定 system、Goal、product、scope、Target、expiry 与 run reference。
- `kym approve baseline`、`kym approve target` 与 `tco approve-coverage` 增加 `--approval-source authorized_system --authorization <path>`；receipt 记录 authorization id 与 digest，不能伪造为 human。
- 两个生成 Skill、workflow contract、KB、人机治理说明与 P05-target-b Automation Goal 输入包同步改为区分人类批准与已绑定系统授权。

### Safety

- 不新增外部 IAM、签名、调度器、自动创建授权或无边界语义批准；缺失、过期、越界或不匹配的授权均 fail-closed。

### Distribution

- 发布固定版本 `v0.1.5`；只更新 `build-kym` 与 `build-tco-coverage` 两个明确选择的 repo-local Skill，保持显式 dry-run、差异保护与安全更新边界。

## [0.1.4] - 2026-07-22

### Added

- 优化 TCO HTML 渲染器：以彩色、多层级、可展开的层次结构展示 TSP、Ref Info、Scope、Purpose、Coverage、Target 与节点详情。
- 增加 Target、Coverage、节点类型、Scope 完整度筛选，以及“全部展开”“只保留 M”“隐藏 Ref Info”交互。
- 保留无 repo 上下文时的完整 TCO 模型树，并增加状态、覆盖词汇和节点层级的可视化元数据。

### Distribution

- 发布固定版本 `v0.1.4`，同步 8 个 repo-local kym-tco Skills；安装仍需显式 dry-run 和 install，不自动覆盖已有不同内容。

### Quality

- 增加渲染器控制项、过滤行为和无 repo 树保留的回归覆盖；通过产品、治理、KB、Dashboard、closeout-language、diff 和构建门禁后再发布。

## [0.1.2] - 2026-07-22

### Added

- Added canonical KYM semantic placement and generation guidance for speaker/actor separation, explicit Target Profiles, and Business Rules parent decomposition.
- Added read-only `kym lint` semantic-risk checks with stable JSON findings and compatibility through the existing audit wrapper.

### Quality

- Added deterministic positive, negative, and false-positive coverage for Target Profiles, role placement, Business Rules shape, and protected KYM Golden behavior.
- Preserved candidate-only and approval-separation boundaries; lint, rendering, and tests do not create KYM revisions or approvals.

## [0.1.1] - 2026-07-21

### Added

- Repeatable `--skill NAME` selection for `tools/install_codex_skills.py`, allowing projects to install only specific repo-local Skills.

### Safety

- Selected Skill names must exist and may be specified only once. Dry-run, explicit install, symlink rejection, non-identical overwrite refusal, and post-copy verification remain unchanged.

## [0.1.0] - 2026-07-20

### Added

- Deterministic `kym` and `tco` CLI workflows for ingest, validation, diff, receipts, verification, and rendering.
- Source-traceable KYM and TCO contracts, canonical knowledge-base data, generated documentation, and regression tests.
- Version-pinned, repo-local Codex Skills with a safe installer that defaults to dry-run, creates only missing Skills, and refuses to overwrite different local content.

### Distribution

- Install from a fixed Git tag. Skill updates require selecting a new tag and running an explicit dry-run and install; no automatic Skill synchronization occurs.

### Boundaries

- Semantic drafting is performed by the `build-kym` and `build-tco-coverage` Skills. The CLI does not automatically approve KYM baselines, Targets, or Coverage proposals.
