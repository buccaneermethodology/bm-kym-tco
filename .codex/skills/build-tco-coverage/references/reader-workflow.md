# 固定离线阅读伴随合同

reader-detail是候选的阅读解释，原KYM/TCO/Coverage及ECQ来源仍为真源。保留原CLI包与技术预览，主入口统一为随Skill发布的reader.html，并交付reader.md、本地SVG和独立原始数据。使用固定navy/浅灰/白卡模板，不每轮重写样式。

## 每次正式生成或更新都完整展开

用户只说“生成TCO”也执行以下要求，不要求用户补长prompt。先盘点实际节点和步骤，选代表能力确认表达尺度，再全量展开并逐M复核。用户要求直接一稿时照做，业务未知具体标出；只读解释旧稿不自动扩成重建。

1. 以实际输入递归建立清单：KYM/TCO命名空间、节点ID、实际TSP字段、全部M、步骤及条件边。共享和未分配M都在清单，不能只检查当前TP筛选结果；Mc不进能力分母。
2. 每个实际TSP都详细解释：Topic说明能力对象和动作；Scope说明边界、输入输出、适用条件和源支持的排除；Purpose说明为谁解决什么问题、为何需要。保留原值，解释不改原Scope。TCO有TSP的Mc/policy也展开；KYM无TSP的普通树标题不新造合同。全部M及KYM-only实际规则能力均须有三项解释，源缺项就列具体未知与最小确认动作。
3. 每一步写清输入、对什么做什么、得到什么结果，以及何时去哪个步骤。明确流程入口、结束结果或持续/停止条件；不按数组首项猜入口。不要仅复述标题、写“同上/详见/完成处理”，也不要为凑字数填通用段落。
4. source_refs绑定来源，inference列推断和根据，unknowns列缺哪项及确认动作。不能编造分支、来源、用户答复、运行状态或停止条件。资料不足可以诚实详细，不等于未实现或0%。
5. 正式伴随文件设置`reading_profile: full_detail.v1`，按同目录reader-detail.schema.json填写node_reading.kym/model、每M的entry_step_ids/entry_description/termination和每step的kind/inputs/result/source_refs/inference/unknowns；原字段完整保留。解释三项来源数组必须存在，不得全空冒充有据。全量审计完成后用`--require-full`验证和渲染。

## 树形、表格与传统流程图

主入口按TP→Mc→M逐层树形展开，显示共享能力与未分配能力。TSP、IPO和步骤用中文字段表；E/C/Q先给量化和文字摘要，随后列真实Session努力、当前质量/缺口和最小下一步。不要把字典、数组或JSON代码块塞进任何主阅读折叠区。

流程图从同一process/transitions生成离线SVG：开始/真实结束用椭圆，处理用矩形，真实判断用菱形，箭头标条件。所有步骤、分支、回路和业务边守恒，多个真实终点都显示；持续流程不虚构终点。图节点和详细步骤双向跳转，Markdown引用同源本地SVG，不只交Mermaid代码。大图可滚动而不裁掉步骤或边；共享M各view的锚点唯一。

复杂分支给具体例子。必要Modeling/UI图只在有来源或显式推断且有助判断时加入；diagram.content保持安全文本，工具生成图，不执行HTML或脚本。全局问题只列一次，局部问题随对应M。

用户明确删除的逐要求进展、逐条件验证与版本适用性、条件原版本/观察时间/复用依据、与上一评估相比、本M前后完整指标、逐要求前后指标与精确归属不在主阅读出现，也不换名放回折叠区。其原始数据、要求分母、历史及Coverage字段仍作为独立机器文件完整交付。原Coverage另用简明真实字段表展示当前轴与TP目标，不能用C功能实现百分比替代运行/验证状态。

## 输入、映射与逐行为判断

bindings绑定实际KYM及可选TCO/Coverage相对路径和原字节sha256。来源变化须重新解释、审计并绑定，不能只改摘要沿用旧结论。intake保留真实guided/direct方式、目的/读者、回答或假设和建议Profile。projections保留源支持Business Rules语义组至共享M的direct/split/merge关系与业务理由，不按Session机械建M。

逐行为判断保留behavior、evidence_summary、evidence_refs、applicability、next_action和acceptance：说明应证明什么、已有证据实际说明什么、还需最小动作和通过条件。共同证据/边界只显示一次，每行独有支持点仍保留。requirement_ids只引用本M真实要求，不能统一套“输入/输出合同”取代独立业务行为。gap_kind只是主要下一步，其他限制仍在证据/适用性中说明；先核旧证据，再判断是否重跑或补最小反例。

无合法TP时交付KYM-only完整阅读和proposed建议，不伪造Profile或空数组TCO v4来凑包。KYM-only省略model/coverage，不能由KYM创造Coverage。Coverage缺失或not_evaluated时允许空requirement映射但解释原因、最小动作和判据，显示未评估而非0/N或缺实现。已评估时全部mandatory要求必须映射。

## 执行与完整性检查

```bash
python3 <skill-dir>/scripts/audit_reader_quality.py --kym <kym.json> --model <tco.json> --coverage <coverage.json> --detail <reader-detail.json>
python3 <skill-dir>/scripts/reader.py validate --workspace <workspace> --kym <kym.json> --model <tco.json> --coverage <coverage.json> --detail <reader-detail.json> --require-full
python3 <skill-dir>/scripts/reader.py render --workspace <workspace> --kym <kym.json> --model <tco.json> --coverage <coverage.json> --detail <reader-detail.json> --require-full --output-dir <new-directory>
```

真实未提供model/coverage时省略对应参数，不制造空文件。结构/绑定/已知简写模式/入口可达/条件/图守恒错误阻断正式交付；声明full也自动严格检查，不传flag不能降级。旧无profile文件仍可在明确只读legacy模式渲染并醒目标识“旧稿仅可读，未按新版完整阅读验收”；旧稿可读不等于正式full合格。

生成方还须逐M交叉核对：原scope的明确排除与解释中的待确认不能互相矛盾，来源沉默不能写成“不包含”；已有检查但缺逐条件绑定，不能概括成“没有检查结果”。这两项检查每次默认执行，不要求用户在prompt重复。

生成方逐M全量复核：TSP是否具体、每步输入动作结果是否清楚、图文同源无漏项、来源/推断/未知是否诚实、E/C/Q和行为判断是否可用于决策。报告实际检查与未观察项。字数、字段存在和相似度都不能单独证明语义正确；审计pass不能创建业务批准。独立审查按宿主/项目合同触发，普通出稿不因没有独立actor或Safari而拒绝生成候选。

工具profile/reader/Skill自身变更和发行须另做独立Validation/Semantic、隔离安装陌生项目前向及真实浏览器验收；这是维护者发布验证，不是业务项目每次出稿都重装工具或再造项目。公开权利和发行authority另按发布合同处理。

## 来源与E/C/Q

UTF-8来源链接进入明确编码、内容转义的安全HTML阅读页，原字节与摘要不改；二进制保留原访问。路径相对可搬移，检查中文空格路径、筛选、树形展开、图文跳转及来源链接；未实际观察的浏览器能力如实记未观察。

E/C/Q数据按build-tco-coverage的ecq-assessment.md整理。E仅真实有证据的Session努力；C主口径是功能实现覆盖估计，Q另报当前质量与资料边界。未整理/未接入不变零；需求本身不证明实现。每次Session更新保留旧快照和原始分母；前后逐要求/条件/归属仍由引擎计算并完整存独立数据，不搬回主阅读技术对照。多M共享候选下一步只维护一份，计划不计E，不自动执行。

```bash
python3 <build-tco-skill>/scripts/ecq.py validate --workspace <workspace> --assessment <ecq.json>
python3 <build-tco-skill>/scripts/ecq.py project --workspace <workspace> --assessment <ecq.json> --output-dir <new-data-directory>
python3 <build-tco-skill>/scripts/reader.py render --workspace <workspace> --kym <kym.json> --model <tco.json> --detail <reader-detail.json> --ecq <ecq.json> --require-full --output-dir <new-reader-directory>
```

需要且实际提供Coverage时加同一真实文件的--coverage。KYM-only不加--ecq；ECQ引擎随TCO Skill发布，不能从缺失/不兼容项目Skill静默切全局副本。主阅读和独立机器数据使用同一验证投影，来源未接入明确not_connected，不回写或批准原候选。建议动作不等于实施授权。

## 产品检查失败时仍提供完整候选阅读

模型/Coverage的结构、语义和原字节绑定通过，而产品verification失败时，继续完成未受阻的完整TSP、流程/IPO、E/C/Q、实际努力和下一步。在全局说明及来源链接中明确包未ready和真实失败。原失败和非零退出保留，建议动作不授权修产品；不得换常过检查或降低目标来凑成功，也不得从full_detail_checked推导包ready或产品通过。没有passing消费bundle时，不能写consumable=true；外部链路原失败仍保留。

若模型、来源、摘要或full门禁自身失败，保留诊断及草稿，不得产生成功full标识或用未知吞错。真实Coverage不得改not_connected来绕过错误；手工估计要明确尚未通过引擎，不能冒称ECQ计算成功。
