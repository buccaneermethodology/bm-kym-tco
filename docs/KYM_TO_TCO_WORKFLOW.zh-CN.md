# 从 KYM 到 TCO：给新用户的原理说明

本文解释 `kym-tco` 的核心工作方式：从一段产品对话、文档或源码观察，怎样得到 KYM，再怎样得到 TCO 和 Coverage preview。重点是区分三类来源：

- LLM/Agent 通过 Skill 起草的语义内容；
- CLI、脚本和测试确定性推导或验证的事实；
- 人类或授权系统显式批准的 authority。

## 先看总流程

```text
输入材料
  -> kym ingest 保存来源快照
  -> Agent 使用 build-kym 起草 KYM
  -> kym validate / lint / audit / render
  -> 人类审查，可选择 approve baseline 与 approve target
  -> Agent 使用 build-tco-coverage 起草 TCO model 与 Coverage proposal
  -> tco validate / verify / audit / compare / render-preview
  -> 人类审查，可选择 approve coverage
```

这条链路里，CLI 不调用 LLM。LLM 只在 Agent 使用 Skill 进行“语义理解、归类、分解、映射”时发挥作用。

## 第一步：输入材料进入 workspace

输入可以是：

- 产品对话；
- 需求文档；
- repo 源码片段；
- 已存在的 KYM/TCO 候选文件；
- 其他明确指定的 source。

`kym ingest` 做的是确定性工作：

- 复制或快照输入；
- 记录 manifest；
- 计算 digest；
- 保存 chunk 或 source reference；
- 建立后续可追溯的 source boundary。

这一步不是 LLM 生成，也不判断语义正确与否。

## 第二步：Agent 使用 build-kym 起草 KYM

KYM 是 Know Your Model / Knowledge Yield Map 风格的知识模型。它把输入中的话拆成可追溯 statement，并放入固定 reader 模板：

```text
Customer
Product
Project
```

Agent 在这一步做 LLM 辅助判断：

- 谁是 Customer、User、Stakeholder；
- 哪些是 Product 信息、命名、范围、排除项、业务规则、标准；
- 哪些是 Project 交付物、工具、计划、团队信息；
- 哪些 statement 是 source supported；
- 哪些只是 inferred；
- 哪些存在 disputed、unresolved 或 superseded 风险。

这一步的 know-how 来自：

- `build-kym/SKILL.md`；
- bundled references，例如 semantic placement、analysis guide、reader quality；
- repo 的 canonical KB 中沉淀出来的可移植规则；
- 过去 Golden fixture、PDI 和审计发现中沉淀的防复发规则。

但最终 KYM draft 仍是 candidate，不是 approved baseline。

## 第三步：KYM 的确定性验证

KYM draft 生成后，CLI 和脚本负责确定性检查：

- `kym validate` 检查 schema、product、revision、source refs 等结构约束；
- `kym lint --strict` 检查固定模板、placement 风险和可审查性；
- `audit_candidate.py` 或 `tools/audit_generation_quality.py kym` 做只读语义风险审计；
- `kym render` 将 JSON 渲染为 Markdown reader。

这些检查能发现格式错误、缺字段、引用不一致、明显 placement 风险。它们不能证明 LLM 的所有语义判断都是唯一正确答案。

## 第四步：KYM approval 的作用

Approve 是人类或授权系统的决定。

```text
KYM draft
  -> approve baseline
  -> approved KYM baseline receipt
```

```text
Target profile candidate
  -> approve target
  -> approved Target receipt
```

Approve 的价值是：

- 锁定当前 revision digest；
- 绑定 source closure；
- 防止来源或 revision 改了以后继续复用旧批准；
- 为后续 TCO 提供明确的输入 authority。

测试通过、Markdown 能打开、Agent 说“看起来不错”，都不能替代 approve。

## 第五步：Agent 使用 build-tco-coverage 起草 TCO

TCO 不是从 KYM 树机械复制出来的。它是 Agent 根据已审查的 KYM/Target 做能力分解。

TCO model 包含：

- `mc`：能力域或组织节点；
- `m1`：一个可独立实现、验证、归责的能力；
- `policy`：约束或规则；
- TSP：每个能力节点的 Topic、Scope、Purpose；
- coverage requirements：每个 M1 要满足哪些证据要求；
- Target/Profile mapping：Target requirement 映射到哪些非 group M 节点。

Agent 在这一步做 LLM 辅助判断：

- 一个业务规则应该映射成什么能力；
- 哪些能力应该拆成多个 M1；
- 哪些只是 MC 分组；
- 哪些是 policy；
- 哪个 Target requirement 对应哪个 M1；
- witness、negative fixture、claim ceiling 应该如何放置；
- 是否存在 Target gap。

TCO 的 know-how 来自：

- `build-tco-coverage/SKILL.md`；
- `tco-contract.md`；
- `tco-modeling-guide.md`；
- `kb/data/tco_generation_knowledge.json` 的可移植规则；
- 历史 PDI、Golden fixture、审计脚本与 renderer 误读修正。

## 第六步：Coverage proposal 的含义

Coverage proposal 是证据叠加层，不是批准。

它回答：

- 当前 repo 有哪些 evidence；
- evidence 支持到什么状态；
- 每个 requirement row 是否有匹配证据；
- 哪些 mandatory requirement 仍缺失；
- claim ceiling 是什么；
- breadth 是单次、单 repo，还是更宽；
- validation posture 是 producer checked 还是 independently validated。

重要边界：

- `coverage_state` 不是 lifecycle；
- `bounded_runtime` 不能写进 lifecycle；
- `maintained` 不能写进 Coverage；
- `verify` 通过不会自动改 Coverage state；
- 一个 M1 的 evidence 不能批量抬升所有 requirement row。

## 第七步：TCO 的确定性验证

TCO 生成后，CLI/脚本负责确定性检查：

- `tco validate-candidate-model` 检查 model schema、node、profile、graph；
- `tco validate-candidate-coverage` 检查 coverage proposal 与 model digest、records、requirement rows；
- `tco verify` 运行 allowlisted verification commands；
- audit script 检查语义风险、evidence mapping、claim ceiling、重复 evidence bundle 等；
- `tco compare-candidate-profiles` 计算 Profile Met/Gap；
- `tco render-preview` 生成 candidate-only HTML。

这些命令能证明“结构合法、命令跑过、结果被记录”。它们不能自动证明“业务语义正确、产品已完成、Coverage 已批准”。

## 第八步：HTML preview 的角色

HTML 是阅读界面，目标是让用户看懂：

- 有哪些 Mc/M1/policy；
- 每个 M1 的 Topic、Scope、Purpose；
- Coverage、lifecycle、validation posture、resolution status 分别是什么；
- 哪些 Target requirement met/gap；
- verify/audit/execution observations 有哪些证据；
- candidate_not_approved 的边界是什么。

HTML 不应该是唯一真源。真正的 authority 分散在：

- KYM JSON；
- TCO model JSON；
- Coverage proposal JSON；
- verification results JSON；
- approval receipt；
- source manifest。

## 哪些由 LLM 生成，哪些由代码推导

| 环节 | 主要来源 | 说明 |
| --- | --- | --- |
| source snapshot | CLI | 确定性保存输入和 digest |
| statement ledger | Agent/LLM | 从输入中拆出语义陈述 |
| KYM placement | Agent/LLM + Skill rules | 判断 Customer/Product/Project 与具体分支 |
| KYM schema validation | CLI | 检查 JSON 合同 |
| KYM lint/audit | 脚本 | 检查固定规则和风险 |
| KYM Markdown | CLI renderer | 从 JSON 确定性渲染 |
| baseline/Target approve | 人类或授权系统 + CLI | 写 receipt，绑定 digest |
| TCO M tree | Agent/LLM + Skill rules | 能力分解和 TSP 设计 |
| Target mapping | Agent/LLM + audit rules | 语义匹配，需要人工审查 |
| Coverage proposal | Agent/LLM + repo evidence | 证据分层和 claim ceiling 判断 |
| Coverage validation | CLI | 检查结构、digest、row completeness |
| verification | CLI | 运行 allowlisted commands |
| Profile Met/Gap | CLI | 从 Target requirement 与 coverage state 确定性计算 |
| TCO HTML | CLI renderer | 从 report 确定性渲染 |
| Coverage approve | 人类或授权系统 + CLI | 写 approved coverage 与 receipt |

## 用户应该关注什么

对小白用户，最重要的是不要混淆三句话：

1. “Agent 生成了候选结果”  
   表示可以审查，不表示正确或批准。

2. “验证命令通过了”  
   表示结构或有界 runtime 检查通过，不表示 Coverage 自动升级。

3. “人类 approve 了”  
   表示这个 artifact 在指定 scope 内成为后续工作的权威输入。

## 最佳体验的目标

当前系统把很多步骤拆得很清楚，但对新用户负担偏高。后续最佳体验 Loop Goal 的目标是：

- 用户只触发一次 Skill；
- Agent 自动检查 CLI 是否可用；
- 自动执行 validate、lint、audit、verify、compare、render；
- HTML 页面集中展示关键结果和链接；
- failure 也生成可读报告；
- approve 仍然保持人工显式动作。

这样既降低使用门槛，也不牺牲 authority 边界。

