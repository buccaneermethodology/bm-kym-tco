# TCO 任务贡献与阶段进展

这是 `tco_progress.v1` 候选伴随格式。它记录稳定 M1 下的推进项，不修改 TCO 模型、Coverage、Target Profile、Dashboard 或批准状态。`completed` 仅指推进项有证据完成，不能推导产品完成率。脚本仅依赖 Python 3.9+ 标准库，加载同一 Skill 中唯一的 reference schema 真源，可复制完整 Skill 到外部 workspace 使用；源码 Skill 仍是 unreleased candidate，文件可随包携带不代表发行版 external-run 已接受它。

## 命令与输入

```bash
python3 <skill>/scripts/progress.py validate --workspace <workspace> --progress <progress.json>
python3 <skill>/scripts/progress.py render --workspace <workspace> --progress <progress.json> --output-dir <new-output-directory>
```

所有输入路径相对 workspace；绝对路径和 symlink 越界均拒绝。`--base-progress` 可显式给出，但必须与文档 `base_progress.path` 指向同一文件；不传时自动解析该引用。相对输出目录以 workspace 为基准；输出目录必须尚不存在，render 生成 `progress.md`、`progress.html`、`audit.json`。失败返回非零且不会留下成功预览。源文件只读。validate 只证明结构、绑定和追加链确定性约束通过，不证明业务语义正确或批准。

字段的完整形状见 `tco-progress.schema.json`。顶层必填字段：

- `schema: tco_progress.v1`、`product_id`、唯一 `snapshot_id`、`lifecycle: candidate_not_approved`、带时区 ISO `cutoff`。
- `included_stages` 是累计纳入阶段，`excluded_stages` 是排除阶段；二者无交集。`tasks` 是本次新增事件所属任务的非空唯一 ID 数组。
- `base_progress` 为 `null` 或 `{path, sha256}`；摘要是文件原始字节的 64 位小写 SHA256，所有证据引用也采用此形状。
- `before` / `after` 各含 `model`、`coverage` 引用与 nullable `report`。模型支持 `kym_tco.tco_model.v2/v3/v4`，Coverage 支持 `.tco_coverage_model.v2/v3`；产品身份必须一致，Coverage 的 `source_tco_model_digest` 必须匹配模型 canonical JSON 摘要。
- `items`：`id/m1_ids/title/acceptance/requirement_ids/owner/next_step/events`。ID 稳定且全局唯一，共享项只有一条，多 M1 明确列入 `m1_ids`；要求关联必须覆盖该项全部 M1。
- `requirements`：`id/text/m1_ids/disposition/reason/evidence_refs`。这是原子要求账本，初始 `retained`；`revised/resolved/excluded` 都需要理由与真实证据。已存在要求不可删除；文本、M1 归属变化必须 `revised`。`excluded` 是保留带理由的历史记录，不是删除。
- `changes`：`kind: node|profile`、`id/reason/evidence_refs`。两侧全部能力 node 或完整 Profile 的新增、移除、内容变化均逐项记录；Profile 要求、阈值、映射及 Target 变化也因此受约束。声明变化不代表该变化获得人类批准。

## 事件与基线链

事件必填 `task_id/stage/observed_at/before/after/outcome/summary/evidence_refs`。状态为 `unknown/not_started/in_progress/partial/blocked/completed`；结果为 `forward/evidence_only/gap_found/regression/no_change`。首个事件从 `unknown` 开始，空事件项仍为 `unknown`，不虚构历史 not_started。每条事件均需真实字节绑定证据，`summary` 说明实际贡献；no_change/evidence_only 不允许修改状态，regression 允许明确回退。forward 必须改变状态且 after 不为 unknown/blocked；blocked 解除后可 forward 到 in_progress/partial，工具不将状态词顺序当作成熟度排名。事件按时间严格递增，不晚于 cutoff，且阶段被纳入、未被排除。每个本次任务至少有一条新增事件；无推进用 no_change 并说明原因。

读取 base 时递归完整校验它及其原始字节绑定；不得循环。base.after 的 model/Coverage 引用必须逐字等于 current.before；product 一致、snapshot_id 不得与任何祖先重复、cutoff 严格递增、累计纳入阶段不得丢失。原 items 不删除，ID 的 M1、来源要求、验收描述和标题不变，历史 events 保留完全相同前缀。新增事件时间须晚于 base.cutoff，属于本次 tasks；历史事件只追溯，不加入本次计数。owner/next_step 可更新。

## 总体 Coverage 与预览

无 report 时整体结果始终是 `unknown（未知）`。有 report 时只投影原 CLI 的 `kym_tco.tco_coverage_report.v1/v2/v3`，须 `report_mode=candidate_not_approved`、product 一致、`source_tco_model_digest/source_coverage_digest` 匹配两侧 JSON 的 canonical digest（`sha256:` 加 sorted keys、UTF-8、ensure_ascii=false、separators=(",", ":") 的 SHA256），Profile 和节点 ID 必须与模型一致。报告 required/effective/lifecycle/validation 状态、Profile goals、缺口均按原字段展示；`not_evaluated` 显示 N/A，不自行算 Met/Gap。报告本身是已绑定 CLI 输出，脚本不重新执行 CLI 或签发 authority；仍需原 CLI 和独立审查核实其生成与语义。

Markdown/离线 HTML 展示前后绑定、M1/Profile 原始分母、Coverage 与能力轴、任务贡献、推进项、要求处置、证据、阻塞和下一步。HTML 的 `#filter-m1`、`#filter-task`、`#filter-stage` 筛选推进项与本次事件。推进项使用全部历史 events 的 task/stage 进行匹配；本次贡献仅使用新增 events。任务下拉包含历史 events 与本次 tasks 的全部任务，并标注“含历史”；阶段下拉使用累计 included_stages。M1 先匹配推进项固定关联，任务和阶段的组合必须命中同一条事件，禁止把不同事件各自命中当成组合命中。空历史项只在未指定任务/阶段时可见；历史事件命中不加入本次贡献计数。共享项在总数中只算一次，`#visible-item-count` 表示当前可见项数。证据链接相对输出目录指向 workspace 内原文件；HTML 转义文本、使用本地脚本，不依赖网络。

`audit.json` 记录来源字节、两侧绑定、本次新增事件及去重 item 数、祖先身份和输出字节摘要。它是可核对投影，不能替代原输入、原 CLI verdict、业务批准或产品验收。
