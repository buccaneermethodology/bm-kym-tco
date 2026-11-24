# 固定离线阅读伴随合同

本伴随文件是已验证candidate的阅读展开，不能取代KYM/TCO/coverage真源。保留原CLI package及preview，再默认交付统一reader的Markdown与HTML；不得每轮临时发明CSS/网页。选用随skill发布的固定navy/浅灰/白卡模板，原生details、键盘焦点、TP/M/文字筛选；模板版本随发行固定。

## 输入与准备

按 references/reader-detail.schema.json 创建reader-detail.json，使用jsonschema校验后运行伴随工具。bindings绑定实际KYM及可选TCO/Coverage相对路径和无前缀sha256；来源字节变化必须重新展开、审计和绑定，不能只改摘要让旧结论继续通过。

- intake保存目的、读者、实际guided/direct模式、实际答复/假设状态、假设与建议Profile。
- projections保存KYM Business Rules语义节点至TCO M的direct/split/merge关系和业务理由；默认直接对应，必要拆并明确说明。不是按来源条目或Session机械建M。
- capabilities保存每M的Input、Process步骤/带条件去向、Output、必要实例、设计图、局部问题与assessment。TSP及状态从candidate读入，不复制第二份真源。
- 全局问题只列一次，局部问题只随对应M显示；共享M在多个TP出现仍引用同一定义。

TCO默认按TP→Mc→M展开。每M有完整TSP、Scope流程图、IPO，Process与流程图用同一份步骤及条件，能从图定位步骤。复杂分支用具体例子。Modeling/UI图仅在能帮助判断且有来源或显式推断时加入；diagram.content用安全文本行图（modeling以A -> B写关系，ui每行一个区块），工具生成可见图，不执行输入HTML/脚本。无法合理刻画时保留待确认，不编造设计。

## 逐行为判断表

无合法canonical TP时按guided-generation的直接一稿路线，运行KYM-only命令，展示详细业务规则及建议区；不要构造target_profiles=[]的TCO v4文件（原schema要求至少一个Profile）。proposed_profiles不是该必需分母的替代物。

每行说明behavior（应证明什么）、evidence_summary（证据实际说明什么）、evidence_refs、applicability（版本/环境/范围）、next_action（最小动作）、acceptance（通过条件）。requirement_ids引用该M真实coverage_requirements，不用“输入合同/输出合同”统一替代有不同反例的业务行为；允许一份报告支持多行，但逐行说明支持点。重要正常/失败/恢复行为均需有处置，不强行套所有类别。

gap_kind仅主要下一步：implementation缺实现、verification缺验证、version_applicability版本适用待核、evidence_binding绑定待完成、business_question业务待确认、not_evaluated未评估、none本行无缺口。它不是综合状态；evidence_summary/applicability仍保留同时存在的其他限制。缺少当前绑定不自动等于代码未实现，历史证据也不自动适用于当前版本。先核旧证据适用性，再判断需不需要重跑或小反例，不能默认增加整套UI测试。

已评估Coverage时逐条映射全部mandatory要求，未知ID/遗漏阻断reader就绪；无Coverage或coverage_mode=not_evaluated允许requirement_ids=[]且gap_kind=not_evaluated/business_question，必须解释未映射原因、下一步与通过条件，显示N/A而非0/N、Met或缺实现。相关TP其他M的缺口必须与本M自身缺口分开，不能成为本M隐含前置。

## 执行

```bash
python3 <skill-dir>/scripts/audit_reader_quality.py --kym <kym.json> --model <tco.json> --coverage <coverage.json> --detail <reader-detail.json>
python3 <skill-dir>/scripts/reader.py validate --workspace <workspace> --kym <kym.json> --model <tco.json> --coverage <coverage.json> --detail <reader-detail.json>
python3 <skill-dir>/scripts/reader.py render --workspace <workspace> --kym <kym.json> --model <tco.json> --coverage <coverage.json> --detail <reader-detail.json> --output-dir <new-directory>
```

仅KYM省略--model/--coverage；新需求未评估Coverage省略--coverage，不伪造记录。detail仍绑定实际输入。工具不运行产品测试、不写回源候选；原CLI检查、阅读审计、语义review和实际浏览器观察分别报告。审计error必须修，advisory逐项判断记录理由，不能仅写“pass”。检查目录搬移到中文空格路径后的来源链接、中文、筛选、展开及图文跳转。所有输出保持candidate_not_approved；建议动作不等于实施授权。
