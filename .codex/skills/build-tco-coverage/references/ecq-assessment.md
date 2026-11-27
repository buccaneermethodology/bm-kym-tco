# E／C／Q候选评估伴随合同

本合同定义 `ecq_assessment.v1`、`contract_revision=r3`、`ecq.rules.r3` 的输入、计算与显示口径。`candidate_not_approved` 表示候选、未经业务批准；结构有效、来源核对或数值投影都不创建产品验收或业务批准。原 TCO、Coverage、progress、Session 与质量来源各自保存真值；伴随文件只保存来源绑定、精确映射、实现评估与投影规则，不保存第二套任务或检查生命周期。

## 一、输入和消费边界

正式 shape 以同目录 `ecq-assessment.schema.json` 为准。所有顶层字段均必填，尚未接入的可空字段仍须明确写出。文件不能含重复 JSON key、NaN、Infinity 或不认识的字段。

```json
{
  "schema": "ecq_assessment.v1",
  "contract_revision": "r3",
  "lifecycle": "candidate_not_approved",
  "product_id": "synthetic-booking",
  "snapshot_id": "s103",
  "cutoff": "2026-01-03T12:00:00Z",
  "base_assessment": {"path": "cases/s102.json", "sha256": "原始字节摘要"},
  "sources": ["见下表的source对象，不是字符串"],
  "scope": {"说明": "选定TP、版本、分母与来源完整性，详见schema"},
  "ruleset": {"说明": "ecq.rules.r3固定规则，详见schema"},
  "capability_catalog": [],
  "business_requirements": [],
  "mappings": [],
  "estimates": [],
  "session_links": [],
  "contributions": [],
  "next_steps": [],
  "quality_checks": [],
  "issues": {"说明": "缺陷来源及字段pointer，详见schema"}
}
```

以上仅是形状导航，字符串说明不是可校验样例。assessment字段见随包schema；跨文件原生对象的最小字段见下文，schema不会自动检查其内容。仓库开发者可在 `tests/fixtures/ecq-p0-r3/cases/s103.json` 查看合成完整样例，发行用户不依赖该测试目录。示例讲解名 M1／M2 对应真实 ID `m1`／`m2`，原始要求为 `m1-r1` 等，以符合既有 CLI 的小写 ID 约束。

| 字段 | 意义与约束 |
| --- | --- |
| `base_assessment` | nullable `{path,sha256}`；绑定上一份不可变评估，用于比较，不覆盖旧快照 |
| `sources` | 唯一 `id`、`kind`、`availability`、`completeness`、nullable `binding` 与 nullable `derivation`；不可用来源不能伪造绑定 |
| `scope` | Goal、纳入阶段、TP IDs、截止点版本／环境、当前模型／Coverage／可空报告来源 ID、原始要求 IDs、四类完整性、权重政策／版本、范围变更引用 |
| `capability_catalog` | M ID 与模型 `nodes` 中精确定义 pointer；包括当前与历史 M，不手填一份新的能力描述 |
| `business_requirements` | 原始业务要求定义、正权重、关键标记、全部必需条件来源、共享组合与职责映射 IDs；包含历史 ledger，但只有 scope IDs 入当前分母 |
| `mappings` | 原始要求→本 M 的明确职责／direct、supporting、boundary→TP requirement→M原子要求→progress item 精确映射 |
| `estimates` | 当前 scope 内职责的 low／high、方法、依据类型／pointer、评估人、时间、适用版本、核心可用性与理由 |
| `session_links` | 原 task ID→Session ID→原 Session 记录 pointer 与 id／status／category／started_at／status_as_of／completed_at 相对字段pointers；不拷贝 Session 状态 |
| `contributions` | 原 event 与所属 item 的双 pointer、本次实际影响的 mapping IDs、贡献类别、可空前后评估 pointer；不复制 event 的状态／outcome |
| `quality_checks` | 每个原始要求的每个职责／必需条件→可空原结果 pointer、适用性来源→固定语义字段的相对 pointers；不写第二套 pass／fail |
| `issues` | 原问题来源 ID、明确的记录 pointers 与字段映射；来源未完整接入时数量为未知 |
| `next_steps` | 唯一id、动作／Session建议、实际mapping归属、来源、产物／验收、依赖／阻断与有据工作量；不进入E、不自动执行 |

`binding.path` 全部相对显式 workspace；`sha256` 为原文件字节的64位小写摘要。`locator={source_id,pointer}` 使用 RFC6901 JSON Pointer，支持 `~0`／`~1` 转义及数组索引，空串指整个对象。`fields` 的 pointer 则相对所定位记录；禁止按标题、名称相似或字段猜测关系。

## 二、来源、非JSON材料与未知

连接状态 `connected` 是已接入，`not_connected` 是未接入，`unavailable` 是声明存在但不可用；后二者 binding 必须 null。完整性 `complete`／`partial`／`unknown` 分别为完整／部分／未知，它是范围说明，不能创造通过或批准。已连接来源必须有真实绑定；已绑定但摘要漂移、产品身份不一致、非法路径或无效 pointer 必须阻断，不能改成普通未知后继续出确定数字。

Session的id pointer必须存在并精确匹配；status、category、started_at、status_as_of、completed_at各允许显式null pointer，表示源未记录且不执行读取。已经提供的任何非null pointer都必须真实有效，错误pointer仍阻断。源字段存在但值为null与未记录pointer分别保留，不补写原数据。缺类别仅使类别未知；缺状态或必要时点使完成／进行中分类未知，已有真实事件E不受影响。

仅已整理业务要求时可以保留 M 的职责名，令 `mapping.resolution=not_connected`、relation 为 unknown、TP／原子／progress 引用为空且给出原因；其实现估计 low／high 均 null。`resolution=unknown` 表示映射无法判定。`exact` 才允许以非空 TP／原子引用进入可计算映射；不能只靠文字说明未知。未知估计必须 `method=unknown`、`basis_kind=unknown`、`basis_ref=null`；禁止只填一个 null 或用0／0.5代替。

真实Session、验收报告和缺陷可以是Markdown／日志。原生JSON可直接pointer读取；派生JSON必须保留derivation：origins的原文binding／selector、规则ID／版本、规则正文binding `extraction_rule_ref`、manual／deterministic、producer／extracted_at、nullable verification_ref。摘要证明所读字节，不独自证明抽取语义或截止点状态。新写Agent摘要不能成为原真源。

抽取规则见schema的 `$defs.extraction_rule`，只允许以下有限语法，不执行任意规则代码。旧 `format=line_json_literal.v1` 的field仅含target_pointer、origin_index、1起算line和prefix；`controlled_records.v1` 的每个field须显式syntax并使用对应shape，不能混入未声明参数。两者均逐字段读取原UTF-8字节，不搜索相似内容，不模糊匹配，不做状态enum翻译。

| syntax | 精确读取规则与拒绝边界 |
| --- | --- |
| `line_json_literal` | 指定行须以prefix开头，其余按严格JSON字面量读取；不去掉未声明内容 |
| `markdown_table` | header_line／separator_line及数据line精确定位；表头trim空格后须与headers顺序完全相等且唯一；separator每格仅`:?---+:?`；首尾必须为竖线且列数相等；数据行的key_column须等于key_value，column须是指定表头，读该格。拒绝转义竖线、嵌套／多行／代码表格及重复key匹配，禁止找其他行替代 |
| `key_value_line` | 指定行以精确prefix开头，prefix须为单一字段名加`=`；其余为完整值，trim两端空格，无引号转义、行内注释、多个键或多行值 |

表格／key=value的value_type限定：string保留非空trim原字符串，boolean只接受小写true／false，null只接受字面null，string_or_null将字面null读为null、其他非空读为字符串；无数值猜测或自定义转换。空格、缺格、缺列不能偷偷造null；源未记录的Session可选字段应使用null pointer，并从规则field与核对清单中明确省略。所给pointer错误仍阻断。表格如`| S103 | done |`的ID／Status不需JSON引号。普通日志`status=PASS`只保留PASS；该值未定义观察含义，投影unknown（未知）并显示原报告，不升级observed_pass。只有原文明确记录受支持状态且全部观察适用资料齐备，才可能计观察分子。

每个规则origin绑定必须与derivation.origins精确相同；选择范围覆盖实际消费行。line_range用L12-L28闭区间，utf8_byte_range用B120-B240半开区间且不切UTF-8字符。规则的每个target_pointer唯一，目标值必须严格类型与原文相等。未支持的原文保持未接入／未知，人工整理缓存仍为derived，不冒充native source。

核对文件见 `$defs.extraction_verification`：rule／output／origins的精确bindings，覆盖全部fields的field_checks，reviewer／reviewed_at。每个check绑定target_pointer、origin_index、line及该值canonical JSON的sha256。引擎必须独立重放，核对规则ID／版本、原文／输出摘要、完整字段集合、定位与值摘要。不能只因为verification_ref非空就认为可信。

manual或deterministic都需要覆盖当前记录／规则／原文／字段的核对才可计观察通过或Session分类；该核对只证明抽取一致，不替代业务批准或独立产品验证。没有核对／核对无关或过旧时标为unverified（未核对），仅显示来源报告，Q不进observed分子、Session分类unknown，已有progress事件E仍保留。合法核对文件但绑定不相关不是可信核对；原文／rule实际摘要漂移、抽取值与原文相反则阻断，不能靠降级放行。

规则覆盖全部实际消费决定字段：Session非null的id／status／category／started_at／status_as_of／completed_at，检查condition_id／status／version／environment／observed_at／observer／independent／expires_at。原始事实及状态适用时点决定cutoff：抽取／核对可在其后处理历史材料，不能把处理时间当原观察时间，也不能借晚处理纳入cutoff后的事实；reviewed_at须不早于extracted_at。derived当前binding、derivation.origins与规则origins、verification三侧bindings一致。verification字段集合不得多／少行，值hash不能替代原值的实际重放。原文、规则、核对和派生JSON均可定位。

## 三、当前范围与逐要求映射

当前 M 集合只能来自 `scope.current_model_source_id` 的 `node_kind=m1`；Mc 不入分母。当前 TP 必须在该模型存在。当前 Coverage 要匹配产品和模型 canonical digest；原 report 非 null 时沿用原 report 的 product／model／Coverage绑定及 TP goals 判据。缺 report 就是 TP 目标未知，不从 C、Q 或 progress.completed 推运行达标。

Coverage可以省略：current_coverage_source_id可指向availability=not_connected、binding=null的明确来源条目，此时原Coverage／TP目标保持未知，不妨碍有独立实现／质量资料支持的E／C／Q已知部分。不能为了通过结构而制造not_evaluated空文件；只有实际已连接的Coverage才检查其product／canonical绑定。没有model时不能定位当前M，按原reader仅KYM边界处理，不猜出M分母。

scope.requirement_ids 必须与 scope.definition_ref 原始清单相等；每条恰有一个 business ledger 定义及正权重。`equal_original_requirements` 要求每条权重1；业务加权必须用 `explicit_business_weights`、来源定义与新 weight_version。分母为空时百分比为 null；完整性为 partial／unknown 时只称“已整理的 N 条要求”，禁止写整个 M 已完成多少。

每个 mapping 要校验：原要求 ID、当前／历史 M ID、TP requirement 的 `source_requirement_ref` 与 `node_ids`、该 M 的 `coverage_requirements.id`、progress requirement 的 `m1_ids`、item 的 `requirement_ids/m1_ids` 和所引用 event 精确对应。supporting／boundary 不能被默认为另一个 direct 成果；若确承载验收职责，其局部份额必须有来源说明。未解决映射按未知处理并列出缺口，不能吞掉父要求。

同一 M 可以属于多个 TP。当前职责映射指向选中 TP 的 requirement；不选中的 TP 不加入当前分母。切换 TP 时展示原始要求集合、权重与 TP 目标变化，不能把门槛更严当实现回退。`tp-alternate` 固定例展示同一 m1 切到另一 TP 后，明确选择 r2／r4，而非悄悄改变 C 算法。

### 直接基线与范围变化

base_assessment同产品、无环、cutoff严格递增且摘要匹配。scope_change_ref只解释直接base的真实差异；没有base不能声称本轮added／removed，初始范围说明用null。存在范围／权重／TP／模型／版本／环境／规则变化必须有该记录；无变化可null或全部空差异。

变更记录含base_snapshot_id／current_snapshot_id，added／removed requirement IDs、added／removed M IDs、weight_changes[{requirement_id,before,after}]，前后TP IDs、product_version、environment、ruleset、model_binding与理由。所有集合差、权重和前后值须由两份绑定assessment复算且一致。没有发生的变更数组为空，不能把旧移除重复登记本轮；模型修订单列binding，不冒充实现进步。只有口径一致时才可显示纯实现百分点差。

## 四、实现份额、共享组合与显示

单个 mapping 的未知份额在计算时投影为[0,1]，文件中仍是 null。已知份额须有限、0≤low≤high≤1，评估时间不晚于 cutoff、适用版本与 scope 相符，且有真实 basis_ref。验收份额来源含 `parts[{id,share,implemented_fraction,core}]`；part 的正 share 总和必须为1，implemented_fraction 在[0,1]，已审阅清单的加权和与 estimate 相符。无法可靠拆分则使用 `reasoned_interval` 并给出不确定性依据，不从 item.partial、代码行数、文件数推50%。不同 method 的依据仍须可定位和审阅。

同一父要求拆成两个四分之一不会增加父权重。M局部区间按其scope内原始要求的本职责份额加权平均；多次Session的“+20%”不直接累加。功能缺陷否定本职责时C必须回退或未知，不能只改Q而C保持100%。

`business_requirement.shared_acceptance`含nullable plan_ref及point_estimate_refs。plan指向原来源的 `$defs.shared_plan`：business_requirement_id、plan_version、来源明确的all_of／any_of及selection_unit、全部mapping_ids及共同points的id／正share／criterion；share总和1。all_of是每个共同点均要求全部职责；any_of只允许原来源明确的完整可替代路径，互补写入／查询不能改任选。

每个point estimate引用 `$defs.shared_point_estimate`：mapping_id／point_id／implemented=true|false|null、basis／assessor／时间／版本／理由。每个plan mapping×point恰有一行；implemented只代表同一最小原子验收点的布尔具备／未具备／未知，不能再把不同比例当点。需进一步拆分才能判断的点保持null。

`all_of`的selection_unit必须为`all_roles_per_point`：逐共同点检查全部职责，全部true为[1,1]，任一false为[0,0]，否则[0,1]；再按点share加权。`any_of`必须为`whole_mapping_path`：先在每个mapping完整路径内按同一组point及share算区间L_m／H_m（true=1，false=0，null=[0,1]），然后父要求区间为[max_m L_m,max_m H_m]。这表示最佳**同一完整路径**的实现份额；不得先逐点OR再相加。份额1只有存在一条全部点true的完整路径才可称完整可用；若无路径全true但仍有未知点，完整可用性未知；全部路径有已知缺点则明确“无完整可用路径”。本期无按验收点选通道的路由能力。

本M局部份额须与该mapping各点share加权的区间相等；每个point的basis须同mapping／point／布尔结果／适用版本／环境，不能只证明pointer存在。按父要求唯一权重进入global C；单mapping直接用本职责estimate。plan映射集合须等于ledger全部职责集合，mapping×point恰一行，IDs区分大小写，不按位置或显示名修复。

多mapping且无计划时，combination=unresolved可以合法保留局部、全局未知；宣称all_of／any_of却无计划，或sidecar与原计划组合不符，必须拒绝。共同点／职责矩阵不完整不能伪造精确值。source中plan与point记录必须按所述schema定义独立验证，不能只验证locator存在。

例：两个等权点A／B，网页只具备A，API只具备B，两条完整路径各50%，any_of父要求为50%，并注明无完整可用路径；不能拼成100%。网页A／B均具备、API仅A时，any_of为100%，可用路径是网页。若两职责是写入／查询共同保证一致，则all_of要求每点两者都具备，互斥半职责例整体0%。没有共同验收语义时可保留局部，global_C=null；未知不是0。

`core_display_guard=core_unavailable_no_main_percent`：任何相关核心行为已知不可用时，主卡优先写“核心行为未具备”，附属进展和合法份额进入详情。核心状态未知时写“核心行为待确认”。这不是统一0.5封顶，不能无业务依据改写实测份额；高份额是否掩盖不可用核心须审查。basis 中的 core 标记／可用性和 estimate 互相矛盾必须拒绝。

`ecq.rules.r3` 固定显示：全部要求未知或分母无效时主数值未知；非全未知且区间宽度>25个百分点时主显“资料不足，暂不能给出可靠整体估算”，详情保留上下界；≤25pp可显示区间。使用未四舍五入的数判断阈值，展示百分数最多1位小数，以 Decimal ROUND_HALF_UP 处理，去除无必要的 `.0`，计算保留原精度。25pp是显示策略，不是产品验收门槛。规则变更必须新 ruleset ID并解释前后差异，不能重算旧快照冒充旧结果。

## 五、努力与前后变化

E 从实际被引用的 progress event 及证据出发，按 task→Session 精确映射，以 Session ID 去重。一个 Session 贡献到多 M／要求，各 M 各计一次；全局只计一次。planned／todo 且无已执行事件不计；doing 可以有真实贡献，但不能因此写已完成。Session分类只读原记录而不复制生命周期：status_as_of必须与cutoff为同一时刻；done还须有非null、可解析的started_at≤completed_at≤cutoff。doing等非终态只有该cutoff快照才能分类。缺时点、快照晚于cutoff或适配核对不足时分类unknown（完成状态待确认），不能反推为doing；已有真实事件仍计E。1月4日done不能套到1月3日；若要回溯，需该cutoff原快照／历史适配。贡献项是否完成另读原item event。未接入执行历史时数值为 null，显示“尚未整理相关Session”；完整已接入且没有合格事件才可以是0。

事件在祖先／后继source中重复出现；完成前缀校验后，逻辑身份固定为首次引入snapshot_id＋item.id＋event序号，并核对canonical event内容。不能把source_id或文档hash加入去重键再把旧前缀重复计数。Session和贡献event分别去重。`forward` 才归正向贡献记录，且按 kind 区分设计与实现；`evidence_only/gap_found/regression/no_change` 分栏计数，不当正向实现。mapping_ids 只列实际发生变化的职责；item关联多要求不等于事件让所有要求前进。一个共享事件拆成多条贡献注释时 event 总数仍去重。耗时和 token 不当人时，本版不凭空生成工时。

base_assessment 必须同产品、cutoff严格递增、无环且摘要匹配。使用前后逐要求值重算 C；并列 scope、权重、ruleset、版本／环境变化，存在口径变化时不给纯实现百分点差。contribution.before／after_estimate_ref 指不可变评估记录，而不是重复手填 event 状态。没有前值时明确未知，不造初始0。

## 六、质量的逐条件判据

Q分母为scope内critical=true且需验收的唯一原业务要求，不是测试数量；每M只纳入它承载的原要求与自身职责必需条件。quality_checks须对每个(required condition,mapping)恰覆盖一次，不可省略困难条件或重复计权。每条check必须有明确mapping_ids，它们全部属于business_requirement_id的真实职责；同一结果可以按真实适用范围覆盖多个mapping，不能复制原检查状态。

result_ref非null时，单职责要求允许以唯一mapping确定归属；共享要求必须有可定位的applicability_ref，否则该职责结果适用性未知、不进观察分子。applicability记录按`$defs.quality_applicability`绑定同一business_requirement_id、condition_id、精确result_ref、所覆盖mapping_ids与理由；check.mapping_ids必须是已证明集合的子集。给了错误归属／错误result／无效pointer则阻断，不靠unknown吞错。多职责确由同一原运行覆盖时可复用一份原结果与适用记录，仍不自动推及其他职责。原始必需条件清单是每职责默认全集，本版不允许用mapping自选子集缩小Q。

例：网页完整路径已观察全部条件通过，API尚未验证。网页Q=1/1，API Q=0/1且待验证；父要求可选网页的实现C=100%，不意味着API Q通过。缺适用证明时保留该M未知；借网页结果给API若与适用来源冲突则拒绝。观察结果、适用证明与抽取核对分别承担不同责任，不由伴随文件新造pass／fail。

标准读取字段为 condition_id、status、version、environment、observed_at、observer、independent、expires_at。实际来源可用不同字段名，通过相对 pointers精确配置；不能通过字段名相似猜出成功。结果允许 `observed_pass`（实际观察通过）、`reported_pass`（仅报告通过）、`not_run`（未运行）、`failed`（失败）、`blocked`（受阻）、`unknown`（未知）；stale（过期）是适用性投影，来源本来的 observed_pass仍保留历史。

只有一条原始要求的全部必需条件都 observed_pass、派生来源的原文／字段核对可信、版本／环境适用（精确匹配或下述有据复用）、观察时间不晚于 cutoff、有真实观察者及原始证据且未过 expires_at，才进入已验证分子。一个条件 observed_pass、另一个 reported_pass不能计该要求；观察通过也不自动称独立验证，独立身份只读 independent及其原责任来源。expires_at为null表示来源未设到期，但不能免除版本／环境检查。

### 原观察结果的版本复用

先核对已有harness／runner／fixture等原证据及本次适用性，再决定是否需要补验证；不能仅因仓库版本号变化默认重跑全部。原结果的执行版本、观察时间、状态与观察者原样保留。每check可用nullable `reuse_applicability_ref`指向`$defs.reuse_applicability`，null且版本／环境不匹配时仍stale（对当前过期）／待核，不删除旧观察。

本版只支持最小精确复用：复用记录精确绑定result_ref、原／当前product_version和environment、original_closure_ref／current_closure_ref、完整性、reviewer／reviewed_at与理由。original_result_closure_pointer必须精确读取原结果当时已保存的闭包locator，且等于original_closure_ref；没有原绑定不能靠事后新摘要补称曾经验证。两份`$defs.artifact_closure`必须来自真实已绑定来源，声明完整闭包并列出全部被观察产物及其消费依赖（含实际harness、runner、fixtures、配置和环境依赖；不能只列某个未变文件）；每个artifact有安全path与原字节sha256。产品身份、environment、mapping_ids、condition_ids、boundary_id及artifact绑定集合全部相同且字节可复查，closure_completeness=complete才可跨版本复用。复用核对时间不得早于原观察、不得晚于cutoff；不改变原观察时间及expires_at，仍检查抽取可信度、职责适用性、观察者和到期。自报“兼容”或来源不全不能当完整闭包证明。

闭包／环境／职责／条件／边界改变或清单partial／unknown时，此最小判据不支持复用，结果保留为原版本历史，当前stale或未知并列出缺口；不自动判failed或强制重跑全部。填了错误result、伪造前后版本／环境、漂移摘要或无效pointer仍是阻断。两closure允许确有差异的合法输入，只使复用不成立。此判据不支持“代码变了但猜测行为相同”的泛化兼容，不产生独立验证或业务批准。

逐条件保留各状态分布。需要单个要求摘要时依次选择：failed→blocked→stale→unknown→not_run→reported_pass→observed_pass；混合 observed_pass／reported_pass归 reported_pass，不能遮掉已通过的局部条件详情。status不认识、源版本缺失或字段不可解析时不得入分子，并给出缺口。

缺少某条件的result_ref或适用绑定，仅表示尚未接入该必需条件的精确适用结果，不等于没有执行检查或没有检查结果。已有检查、PASS/FAIL与当前逐条件分子分别说明并保留来源；缺口写“尚缺与本条件精确绑定的适用观察/标准结果”，不能一面链接真实失败一面写“尚无原检查结果”。不改变Q分子、状态或原结果来消除措辞矛盾。

质量主状态优先：相关关键验收失败或未解 blocking／critical问题→`acceptance_blocked`（有验收阻断）；来源／范围／必需条件清单无法形成判断→`insufficient_evidence`（证据不足）；已有实现但必需验证未齐→`pending_verification`（待验证）；只在规定来源完整、所有必需条件适用观察通过且没有未解阻断时→`no_blocker_observed`（本范围未发现阻断）。不得以“没有登记问题”变绿。

缺陷必须有明确的 M／原始要求影响集合；无关 M 不被自动污染。问题清单不完整时数量 null，不能写0。invalidates_implementation=true 的问题须核对并使对应 C 回退／未知；可维护性等不改变功能范围的问题仅改变 Q。这里不提供综合质量分，不生成产品批准。

## 七、历史 M 的受限只读适配

`strict_event_archive_v1` 直接保留原始 progress snapshots与当时 model／Coverage绑定，不重写原 events，也不要求历史 M 在今天的 model 中仍存在。capability_catalog 通过旧 model pointer说明过去的 M；当前model排除该 M时，它只进入历史 E／时间线，当前 C为null，当前分母也排除它。移除后再下一次评估仍保留同一历史来源，不能靠当前 before／after并集推全历史。

适配器必须对被消费的完整祖先链检查：所有字节摘要／路径、产品与候选身份、model／Coverage canonical绑定、snapshot唯一且无环、cutoff严格递增、before与上次after连续、stage累计、items／requirements保留、item稳定语义与event逐字前缀、event before／after和严格时间／任务归属、证据摘要，以及 M 在事件当时模型中的真实定义与要求归属。缺少祖先时历史完整性必须降为partial并列缺失，不能声称全历史已接入。

旧版 progress.py（0.4.7及以前）的当前 before／after并集校验可能在移除 M后的后继快照拒绝历史 item。0.4.8已修复同一对象经完整直接基线连续继承的合法历史；读取时分别保留当前工具实测结果与来源中的旧版本报告，不强迫当前工具重现旧失败。ECQ仅对“该 M曾在绑定祖先中存在、仅遗留在不可变历史事件／ledger、移除后无冒充当前新贡献”提供受限适配；不能豁免摘要、身份、时间、前缀或从未存在的 M等错误。没有有效当时归属证明就阻断。若用户提供原工具报告，可将报告作为source绑定投影，保留原failed结果，不新造手填source_validation。ECQ自己的严格检查通过不等于原progress工具通过，也不扩大相邻历史修复的完成声明。

历史链应覆盖真实移除发生点与移除后的后继：移除发生点before含该M、after排除；下一快照before／after都排除而旧事件前缀保持，新事件只归仍有效M。旧M历史E保留，当前C=null且不入当前分母；不得再次登记相同移除。原progress工具拒绝旧ledger时保留其failed（原工具失败）结果，受限适配不改写该事实，也不扩大其他历史修复的完成声明。

## 八、确定性门禁与消费边界

JSON Schema使用自含 `$defs/$ref/anyOf` 与基本对象／数组／数值约束，无远端引用。CLI已有jsonschema>=4.18，可在可选ECQ路径复用成熟validator；原progress.py轻量shape不校验number／boolean／minimum，不能拿它替代本schema的完整数值检查。无需另造通用schema框架。离线分发须携带本schema；不使用ECQ时原reader／progress标准库路径不受影响。

结构后仍须执行：来源安全／摘要／pointer、唯一ID、正权重／scope完整性、原要求→TP／M／原子／item／event映射、时间／版本／有限数、份额basis、核心护栏、逐必需条件与适用性、前后链／历史受限适配。这些语义约束不全能由JSON Schema表达。任一阻断应返回非零、可定位错误、无成功投影；检查失败不能被“未知”吞掉。负例的error code是首要匹配类别，允许同时报告其他真实问题。

消费端应先validate再project：所有阻断通过前不输出成功投影。reader可选绑定该伴随文件，主阅读用同一投影呈现三卡、真实努力、质量/缺口与下一步；逐要求/逐条件/前后/历史完整数据另存独立文件，不回流主阅读技术对照。未提供伴随文件时明确未知，不从Coverage轴硬推C；同源KYM、模型、Coverage与detail仍按各自既有合同消费。来源／schema检查不替代计算oracle、语义拒绝、浏览器观察或离线验证。

next_steps是Agent建议：唯一id、kind=action|session、title／reason、实际mapping_ids、input_refs、expected_outputs／acceptance_criteria、dependencies／blockers与nullable effort。每个mapping都须有至少一个input_ref：精确等于该原始业务要求的definition_ref，或所指对象的id精确等于该mapping ID或原始业务要求ID；只写mapping_id而没有id的说明对象不能满足此检查。共享候选只存一条，多M通过真实mapping引用；输入必须支持全部归属，不得把未影响M随意拉入计划。effort只有有据时才填low≤high及person_hours／person_days和basis_ref；依赖其他next_step ID须存在且无环，外部条件写blockers。计划不进入E、不自动建Session／执行／改目标、不承诺C增长；可读原reader-detail行动／验收输入但不复制事实真值。例如一次共享Session建议可绑定m1-r2与m2-r1，但两侧输入和职责必须均有来源。

主显示、详情与证据链接必须同一数据投影；同一共享候选只存一份，不能自动创建Session、执行任务、改变目标或承诺C增长。

## 九、手算与证据界限

四条等权要求的实现份额为1、1、0.5、未知时，C=(1+1+0.5+[0,1])/4=[0.625,0.875]；E是来源中不同实际Session数，不能用该C反推。若四条关键要求只有一条全部条件适用观察通过，Q_observed=1/4；这不是综合质量分或业务批准。

开发者合成先验位于仓库tests/fixtures/ecq-p0-r3，发行用户仅需本合同、随包schema及自身真实来源。人工oracle必须独立于被验证计算逻辑；来源结构通过、原文抽取核对、ECQ计算一致、独立验证、产品验收与业务批准分别报告，不能相互替代。

## 十、逐M职责与整体要求的阅读范围

每M当前逐要求列表只取该M的当前requirement_ids；原ledger保留的范围外要求不能成为当前未知缺口。直接前后比较使用各快照当时的本M局部C／Q与精确归属，以两侧并集展示新增／移出；不按ID前缀或要求名称猜M。全局原要求整体变化只在独立全局数据保留一次，不能套进每M当作局部指标或恢复主阅读技术对照。历史M不在当前分母，但之前职责与贡献仍可追溯。

合法的current_coverage_source_id可指向not_connected且binding=null的Coverage来源；它不表示已有文件。无Coverage时仍展示有据E／C／Q，不从不存在的Coverage推状态。已连接Coverage则必须与reader实际输入原字节一致；不通过伪造空文件或补摘要绕过。

## Coverage的模型内容身份

已接入Coverage必须保留product_id、candidate_not_approved及source_tco_model_digest，并对应至少一个按原字节绑定、同产品的候选model；当前Coverage还必须绑定scope明确选中的当前model。canonical算法与CLI一致。source_tco_model只保留原来源名称提示，不按basename猜身份或跟随它打开新路径。完整相对名称及同canonical内容的多个绑定别名可以合法存在，不增加M或要求分母。错/未绑定摘要、字节漂移、错产品/候选或历史代当前仍阻断；不能重写摘要或断开真实Coverage隐藏错误。

## 原生引用对象的最小读取合同

locator的source_id选定整份来源，pointer再选其中真实对象；“ID相同”不能替代来源类型和所属M。以下是现有消费合同，不是新的业务要求，也不增加assessment字段。

| 引用字段 | 来源与必须指到的对象 | 常见混淆 |
| --- | --- | --- |
| capability_catalog.definition_ref | model的nodes中实际M对象 | 不能复制一份无来源能力定义 |
| mappings.tp_requirement_refs | model的target_profiles中的requirements对象 | 不是business ledger或progress要求 |
| mappings.atomic_requirement_refs | model的nodes中coverage_requirements对象 | 不能指progress.requirements；它们即使ID一样也是不同对象 |
| mappings.progress_item_refs | progress的items对象 | 其requirement_ids再关联同一progress的requirements |
| business_requirements.definition_ref | 原业务要求定义对象，含id及conditions | conditions中每项须有唯一id，必需条件不可省 |
| scope.definition_ref | 原始要求清单所在对象，含requirement_ids | 不能用当前TP筛选结果重建原始分母 |
| estimates.basis_ref | 本mapping的量化估计依据对象，详见下表 | 不是只含代码路径/parts的说明，也不是estimates自己 |

`atomic_requirement_refs`定位model.nodes中本M的coverage_requirements实际要求对象；pointer里的两个数组索引以实际model为准，不能假定首节点是M。progress要求与model原子要求之间仍按既有精确ID/职责关系核对，不能互换引用。

量化basis对象由来源记录提供；如果是本次代码审阅或候选分析新写的记录，要标明作者、来源、推断与限制，不冒称用户原话或历史执行。引擎实际读取的最小字段如下：

| 字段 | 合同 |
| --- | --- |
| id | 值为本mapping的id；当原业务要求仅有一个mapping时也允许该业务要求id。字段名是id，不能仅写mapping_id代替 |
| low、high | 两值须显式存在并与estimates本行一致；已知份额为0至1有限数。只在estimates写数字而basis缺字段仍会失败 |
| core_behavior | 与本行一致的known_available、known_unavailable或unknown，需具体依据 |
| parts | acceptance_share方法必需；每项唯一id、正share、0至1 implemented_fraction和布尔core。share合计1，加权和必须等于low/high |
| 说明与追溯 | 具体理由、代码/证据绑定与适用限制供语义复核；数字相等不证明实现事实 |

reasoned_interval同样须在basis保留id、low/high、core_behavior与区间依据。未知估计用method=unknown、basis_kind=unknown、low/high=null、basis_ref=null，不编造已知basis或默认百分比。其他原生对象如shared_plan、shared_point_estimate、scope_change、extraction_verification、闭包及适用性记录，使用随包schema相应$defs；Session和检查结果的可变原字段通过既有fields相对pointer读取，不另造状态事实。
