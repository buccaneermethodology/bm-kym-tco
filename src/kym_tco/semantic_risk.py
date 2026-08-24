from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .common import ContractError, digest_value, load_json, project_root
from .validation import validate_document
from .workspace import load_config, state_rank


Finding = dict[str, Any]


def _normalize_text(value: str) -> str:
    return " ".join(value.split()).casefold()


def _canonical_token(value: str) -> str:
    return re.sub(r"[-_\s]+", "_", value.strip().lower())


V4_INPUT_ONLY_TOKENS = {"candidate_input"}
V4_OUTPUT_ONLY_ARTIFACT_TOKENS = {
    "p06assessment",
    "p06_assessment",
    "p06validationreport",
    "p06_validation_report",
    "p06decisionrecord",
    "p06_decision_record",
}
CAPABILITY_LIFECYCLE_STATES = {"draft", "contract_defined", "implemented", "maintained", "deprecated"}
VALIDATION_POSTURES = {"not_validated", "producer_checked", "independently_validated"}
RESOLUTION_STATUSES = {"resolved", "unresolved", "disputed"}
BREADTH_LEVELS = {"single_trial", "single_repo", "known_family", "multi_repo", "cross_language", "arbitrary_attempt"}
COVERAGE_AXIS_FORBIDDEN_VALUES = CAPABILITY_LIFECYCLE_STATES | VALIDATION_POSTURES | RESOLUTION_STATUSES | {"multi_repo_evidence"}
LIFECYCLE_AXIS_FORBIDDEN_VALUES = (
    VALIDATION_POSTURES
    | RESOLUTION_STATUSES
    | BREADTH_LEVELS
    | {"multi_repo_evidence", "bounded_runtime", "integrated_runtime", "test_bound", "schema_validator", "design_only", "not_started", "unknown"}
)


def _walk_kym_tree(node: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [node]
    for child in node.get("children", []):
        rows.extend(_walk_kym_tree(child))
    return rows


def _statement_refs(node: dict[str, Any]) -> set[str]:
    refs = set(node.get("statement_refs", []))
    for child in node.get("children", []):
        refs.update(_statement_refs(child))
    return refs


def _finding(
    code: str,
    severity: str,
    artifact_kind: str,
    path: str,
    subject_ids: list[str],
    message: str,
    evidence: dict[str, Any],
    *,
    confidence: str = "high",
    resolution_required: bool | None = None,
    current_node_ids: list[str] | None = None,
    suggested_node_ids: list[str] | None = None,
) -> Finding:
    if resolution_required is None:
        resolution_required = severity == "error" or confidence == "high"
    return {
        "code": code,
        "severity": severity,
        "artifact_kind": artifact_kind,
        "path": path,
        "subject_ids": sorted(set(subject_ids)),
        "message": message,
        "evidence": evidence,
        "confidence": confidence,
        "resolution_required": resolution_required,
        "current_node_ids": sorted(set(current_node_ids or [])),
        "suggested_node_ids": sorted(set(suggested_node_ids or [])),
    }


def _sorted_findings(findings: list[Finding]) -> list[Finding]:
    severity_order = {"error": 0, "warning": 1}
    return sorted(
        findings,
        key=lambda item: (
            severity_order[item["severity"]],
            item["code"],
            item["artifact_kind"],
            item["path"],
            tuple(item["subject_ids"]),
            item["message"],
            json.dumps(item["evidence"], ensure_ascii=False, sort_keys=True),
        ),
    )


def _report(
    *,
    report_kind: str,
    artifact_kind: str,
    artifact_digest: str,
    base_digest: str | None,
    findings: list[Finding],
) -> dict[str, Any]:
    ordered = _sorted_findings(findings)
    return {
        "report_kind": report_kind,
        "candidate_only": True,
        "artifact_kind": artifact_kind,
        "artifact_digest": artifact_digest,
        "base_digest": base_digest,
        "error_count": sum(item["severity"] == "error" for item in ordered),
        "warning_count": sum(item["severity"] == "warning" for item in ordered),
        "findings": ordered,
        "authority_note": (
            "该只读报告只标识候选风险，不创建批准或 receipt，也不证明业务语义正确。"
        ),
    }


def _require_product(document: dict[str, Any], product_id: str, label: str) -> None:
    if document.get("product_id") != product_id:
        raise ContractError(f"{label} product_id 与请求不一致")


def _node_index(document: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, str | None]]:
    node_by_id: dict[str, dict[str, Any]] = {}
    parent_by_id: dict[str, str | None] = {}

    def visit(node: dict[str, Any], parent: str | None) -> None:
        node_by_id[node["id"]] = node
        parent_by_id[node["id"]] = parent
        for child in node.get("children", []):
            visit(child, node["id"])

    visit(document["tree"], None)
    return node_by_id, parent_by_id


def _node_branch(node_id: str, parent_by_id: dict[str, str | None]) -> str:
    current = node_id
    parent = parent_by_id.get(current)
    while parent not in {None, "customer", "product", "project"}:
        current = parent
        parent = parent_by_id.get(current)
    return current


def _statement_by_id(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in document["statements"]}


RULE_LIKE_RE = re.compile(
    r"(必须|不得|不能|禁止|应当|应该|应保持|保持|只允许|需要|要求|requires?|must|shall|should|never|forbid|prohibit)",
    re.IGNORECASE,
)
ARTIFACT_ONLY_RE = re.compile(
    r"(artifact|report|markdown|json|document|fixture|evidence|产物|报告|文档|夹具|证据)",
    re.IGNORECASE,
)
MULTI_AUTHORITY_RE = re.compile(r"(,|;|/|、|\band\b|\bor\b| 和 | 与 )", re.IGNORECASE)
NEGATIVE_RE = re.compile(r"(不|不得|不能|禁止|排除|不包含|不进入|不修改|exclude|excluded|without|not\s+in)", re.IGNORECASE)
POSITIVE_RE = re.compile(r"(包含|覆盖|进入|支持|提供|允许|include|included|support|provide|allow)", re.IGNORECASE)
TIME_RE = re.compile(
    r"(今天|明天|昨天|本轮|下一|阶段|迭代|里程碑|计划|持续|先|后|before|after|phase|iteration|milestone|schedule|next)",
    re.IGNORECASE,
)
ARTIFACT_RE = re.compile(
    r"(`[^`]+`|\.json|\.md|\.py|\.txt|\.yaml|\.yml|\.pdf|报告|文档|fixture|package|receipt|script|CLI|JSON|Markdown)",
    re.IGNORECASE,
)
CAPABILITY_RE = re.compile(r"(能力|支持|提供|保持|允许|可审计|并行|generate|support|provide|enable|allow|capability)", re.IGNORECASE)
DOCUMENT_RE = re.compile(r"(文档|资料|参考|书稿|模板|PDF|Markdown|README|document|manual|template|source)", re.IGNORECASE)
STANDARD_RE = re.compile(r"(schema|标准|规范|协议|contract|format|standard|policy|compliance)", re.IGNORECASE)
PROBLEM_RE = re.compile(r"(未决|未解决|待确认|问题|不清楚|unresolved|unknown|question|gap)", re.IGNORECASE)
TEAM_RE = re.compile(r"(团队|角色|reviewer|agent|lane|validation|semantic|builder|customer|user|stakeholder)", re.IGNORECASE)
ACTOR_RE = re.compile(
    r"(客户|用户|使用者|工程师|操作员|团队|组织|部门|负责人|利益相关者|"
    r"customer|user|operator|engineer|team|organization|department|stakeholder)",
    re.IGNORECASE,
)
# 这些正则只用于 candidate lint 的可解释提示，不把关键词命中当成
# 产品事实或批准依据。Target 触发必须同时出现 A/B 两条明确标签，避免
# 普通文档中的 ``Target A`` profile 或 calibration 文字造成误报。
EXPLICIT_TARGET_B_RE = re.compile(
    r"\btarget\s*[-_ ]?b\s+mainline\b|\btarget[-_ ]?b\s+mainline\b",
    re.IGNORECASE,
)
EXPLICIT_TARGET_A_RE = re.compile(
    r"\btarget\s*[-_ ]?a\s+(?:calibration\s*(?:and|/)\s*promotion|calibration)"
    r"(?:\s+side[-_ ]?lane)?\b",
    re.IGNORECASE,
)
PRODUCT_DIRECTION_RE = re.compile(
    r"(产品方向|方向决策|产品决策|治理|授权|受影响|受益|竞争关系|product\s+direction|"
    r"decision[-_ ]?maker|governance|authori[sz]ation|affected|benefit)",
    re.IGNORECASE,
)
GENERIC_SPEAKER_RE = re.compile(
    r"^(?:user|assistant|system|speaker|human|codex|用户|助手|系统|发言者)$",
    re.IGNORECASE,
)
PSEUDO_TCO_RE = re.compile(
    r"(?:^|[-_\s])(m\d{1,3}|tsp|tco|coverage(?:[-_\s]?(?:tree|model|state|proposal))?)(?:$|[-_\s])",
    re.IGNORECASE,
)


FIXED_SECOND_LEVEL = {
    "customers",
    "users",
    "stakeholders",
    "info",
    "naming_conventions",
    "scope",
    "exclusions",
    "business_rules",
    "documents",
    "problems_unsolved",
    "standards",
    "deliverables",
    "equipments_and_tools",
    "schedule",
    "team",
}


INCOMPATIBLE_MULTIHOME = {
    frozenset({"naming_conventions", "business_rules"}),
    frozenset({"naming_conventions", "target_profiles"}),
    frozenset({"business_rules", "target_profiles"}),
    frozenset({"scope", "exclusions"}),
    frozenset({"documents", "business_rules"}),
    frozenset({"deliverables", "business_rules"}),
    frozenset({"team", "business_rules"}),
}


def _is_term_like(title: str) -> bool:
    title = title.strip()
    if not title:
        return False
    if RULE_LIKE_RE.search(title):
        return False
    words = re.findall(r"[\w\u4e00-\u9fff]+", title, re.UNICODE)
    if len(words) <= 4:
        return True
    if re.search(r"^[A-Z][A-Za-z0-9_-]{1,24}$", title):
        return True
    if "`" in title or "\"" in title or "“" in title:
        return True
    return False


def _document_texts(document: dict[str, Any]) -> list[str]:
    """Return user-visible candidate text used for explicit-context gates."""

    texts = [str(item.get("text", "")) for item in document.get("statements", [])]
    texts.extend(str(node.get("title", "")) for node in _walk_kym_tree(document["tree"]))
    texts.extend(str(profile.get("label", "")) for profile in document.get("target_profiles", []))
    return texts


def _has_explicit_target_pair(document: dict[str, Any]) -> tuple[bool, dict[str, list[str]]]:
    """Only explicit Target-B Mainline + Target-A calibration labels trigger completeness."""

    b_hits = [text for text in _document_texts(document) if EXPLICIT_TARGET_B_RE.search(text)]
    a_hits = [text for text in _document_texts(document) if EXPLICIT_TARGET_A_RE.search(text)]
    return bool(b_hits and a_hits), {"target_b": sorted(set(b_hits)), "target_a": sorted(set(a_hits))}


def _audit_kym_target_profiles(document: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    explicit, context = _has_explicit_target_pair(document)
    if not explicit:
        return findings

    statement_ids = {item["id"] for item in document.get("statements", [])}
    statements = _statement_by_id(document)
    profiles = document.get("target_profiles", [])

    profile_ids = [str(profile.get("id", "")) for profile in profiles]
    duplicate_profile_ids = sorted({item for item in profile_ids if profile_ids.count(item) > 1 and item})
    if duplicate_profile_ids:
        findings.append(
            _finding(
                "KYM_TARGET_PROFILE_DUPLICATE_ID",
                "error",
                "kym",
                "/target_profiles",
                duplicate_profile_ids,
                "Target Profiles 的 id 必须唯一，避免后续 Target/receipt 绑定歧义。",
                {"duplicate_ids": duplicate_profile_ids},
                confidence="high",
                current_node_ids=duplicate_profile_ids,
                suggested_node_ids=["target_profiles"],
            )
        )

    def matches(profile: dict[str, Any], target: str) -> bool:
        haystack = f"{profile.get('id', '')} {profile.get('label', '')}".casefold()
        return bool(re.search(rf"target\s*[-_ ]?{target}\b", haystack))

    expected = {target: [profile for profile in profiles if matches(profile, target)] for target in ("a", "b")}
    for target in ("a", "b"):
        if expected[target]:
            continue
        findings.append(
            _finding(
                "KYM_TARGET_PROFILE_MISSING",
                "warning",
                "kym",
                "/target_profiles",
                ["target_profiles"],
                f"输入明确要求 Target-{target.upper()}，但候选未提供对应 Target Profile。",
                {"target": f"target-{target}", "explicit_context": context},
                confidence="high",
                current_node_ids=["target_profiles"],
                suggested_node_ids=[f"target-{target}"],
            )
        )

    for target, target_profiles in expected.items():
        for profile in target_profiles:
            profile_id = str(profile.get("id", f"target-{target}"))
            path = f"/target_profiles/{profile_id}"
            missing_fields = [
                field
                for field in ("objective", "requirement_refs", "claim_ceiling", "exclusions")
                if not profile.get(field)
            ]
            invalid_refs = sorted(set(profile.get("requirement_refs", [])) - statement_ids)
            blocked_refs = sorted(
                ref_id
                for ref_id in profile.get("requirement_refs", [])
                if ref_id in statements
                and (
                    statements[ref_id].get("support_status") in {"disputed", "unresolved", "superseded"}
                    or "naming_conventions" in statements[ref_id].get("tree_node_ids", [])
                )
            )
            if not missing_fields and not invalid_refs and not blocked_refs:
                continue
            findings.append(
                _finding(
                    "KYM_TARGET_PROFILE_INCOMPLETE",
                    "error",
                    "kym",
                    path,
                    [profile_id, "target_profiles"],
                    f"明确 Target-{target.upper()} 的 Profile 合同不完整，必须有 objective、requirement_refs、claim ceiling 和 exclusions。",
                    {
                        "target": f"target-{target}",
                        "missing_fields": missing_fields,
                        "invalid_requirement_refs": invalid_refs,
                        "blocked_requirement_refs": blocked_refs,
                    },
                    confidence="high",
                    current_node_ids=[profile_id],
                    suggested_node_ids=["target_profiles"],
                )
            )
    return findings


def _audit_kym_business_rules_shape(document: dict[str, Any]) -> list[Finding]:
    """Check hierarchical rule shape without treating Business Rules as a TCO model."""

    findings: list[Finding] = []
    node_by_id, _parent_by_id = _node_index(document)
    branch = node_by_id.get("business_rules")
    if branch is None:
        return findings

    children = list(branch.get("children", []))
    leaves = [child for child in children if child.get("children") == []]
    direct_refs = list(branch.get("statement_refs", []))
    total_rule_leaves = len(leaves) + len(direct_refs)
    # 旧版 KYM 的普通 flat rule list 仍是合法 candidate 形状；本增量只
    # 对 P05 Target-B/candidate-governance 语境要求核心主题父级，避免对
    # TaskMonitor 等受保护 Golden 施加新的结构性语义。
    p05_context = bool(
        re.search(
            r"(target\s*[-_ ]?b|candidate|provider[-_ ]?backed|diagnostic|promotion|governance)",
            " ".join(_document_texts(document)),
            re.IGNORECASE,
        )
    )
    # 多个同层 rule leaf 或根节点直接挂多个 statement，说明缺少核心主题父级。
    if p05_context and total_rule_leaves >= 2 and not children:
        findings.append(
            _finding(
                "KYM_BUSINESS_RULES_OVER_FLAT",
                "warning",
                "kym",
                "/tree/business_rules",
                ["business_rules", *direct_refs, *(child["id"] for child in leaves)],
                "Business Rules 规则叶子过度平铺；应先按核心主题建立父级，再挂条件/约束叶子。",
                {"direct_statement_count": len(direct_refs), "direct_leaf_count": len(leaves)},
                confidence="high",
                current_node_ids=["business_rules", *(child["id"] for child in leaves)],
                suggested_node_ids=["business_rules:p05-target-b-candidate-governance"],
            )
        )
    elif p05_context and total_rule_leaves >= 2 and children and all(child.get("children") == [] for child in children):
        findings.append(
            _finding(
                "KYM_BUSINESS_RULES_PARENT_MISSING",
                "warning",
                "kym",
                "/tree/business_rules/children",
                ["business_rules", *(child["id"] for child in children)],
                "Business Rules 有多个同层节点但缺少核心主题父级分解。",
                {"child_ids": sorted(child["id"] for child in children)},
                confidence="high",
                current_node_ids=["business_rules", *(child["id"] for child in children)],
                suggested_node_ids=["business_rules:p05-target-b-candidate-governance"],
            )
        )

    # A named P05 governance parent is not enough if it merely becomes a new
    # flat bucket for every rule.  Require topic children once the parent has
    # several direct statements; ordinary non-P05 KYM and one-off rules remain
    # unaffected.
    for child in children:
        marker = f"{child.get('id', '')} {child.get('title', '')}"
        direct_child_refs = list(child.get("statement_refs", []))
        if (
            p05_context
            and len(direct_child_refs) >= 2
            and not child.get("children")
            and re.search(r"(p05|candidate|governance|provider|promotion)", marker, re.IGNORECASE)
        ):
            findings.append(
                _finding(
                    "KYM_BUSINESS_RULES_PARENT_FLAT",
                    "warning",
                    "kym",
                    f"/tree/business_rules/{child['id']}/statement_refs",
                    [child["id"], *direct_child_refs],
                    "P05 Business Rules 语义父节点不应只是平铺规则容器；应继续按主题建立子节点。",
                    {
                        "parent_id": child["id"],
                        "direct_statement_count": len(direct_child_refs),
                    },
                    confidence="high",
                    current_node_ids=["business_rules", child["id"], *direct_child_refs],
                    suggested_node_ids=[
                        f"{child['id']}:candidate-output-contract",
                        f"{child['id']}:diagnostics",
                        f"{child['id']}:provider-and-promotion",
                    ],
                )
            )

    # 只对节点标题/id 中明确的 TCO/M/TSP/Coverage 形状报警，避免规则正文
    # 讨论“不得生成 TCO”时被误报。
    for node in _walk_kym_tree(branch):
        if node is branch:
            continue
        marker = f"{node.get('id', '')} {node.get('title', '')}"
        if not PSEUDO_TCO_RE.search(marker):
            continue
        findings.append(
            _finding(
                "KYM_BUSINESS_RULES_PSEUDO_TCO",
                "warning",
                "kym",
                f"/tree/business_rules/{node['id']}",
                [node["id"]],
                "Business Rules 节点看起来像 M/TSP/Coverage/TCO artifact，不应替代 TCO model。",
                {"node_id": node["id"], "title": node.get("title", "")},
                confidence="high",
                current_node_ids=[node["id"]],
                suggested_node_ids=["business_rules", "scope", "deliverables"],
            )
        )
    return findings


def _audit_kym_semantic_placement(document: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    statements = _statement_by_id(document)
    node_by_id, parent_by_id = _node_index(document)

    naming = node_by_id.get("naming_conventions")
    if naming is not None and naming.get("statement_refs"):
        findings.append(
            _finding(
                "KYM_NAMING_ROOT_DIRECT_STATEMENT",
                "error",
                "kym",
                "/tree/naming_conventions/statement_refs",
                ["naming_conventions", *naming["statement_refs"]],
                "Naming Conventions 根节点不应直接挂 statement；应先建立具体术语子节点。",
                {"statement_refs": sorted(naming["statement_refs"])},
                current_node_ids=["naming_conventions"],
                suggested_node_ids=["naming_conventions:<term>"],
            )
        )
    if naming is not None:
        for child in naming.get("children", []):
            if not _is_term_like(child["title"]):
                findings.append(
                    _finding(
                        "KYM_NAMING_CHILD_NOT_TERM_LIKE",
                        "warning",
                        "kym",
                        f"/tree/naming_conventions/{child['id']}",
                        [child["id"]],
                        "Naming Conventions 的直接子节点不像术语项，请确认是否把规则或说明段落当作术语。",
                        {"child_title": child["title"]},
                        confidence="low",
                        resolution_required=False,
                        current_node_ids=[child["id"]],
                        suggested_node_ids=["business_rules", "info"],
                    )
                )

    statement_branches: dict[str, set[str]] = {}
    for statement in document["statements"]:
        for node_id in statement["tree_node_ids"]:
            statement_branches.setdefault(statement["id"], set()).add(_node_branch(node_id, parent_by_id))
    for statement_id, branches in sorted(statement_branches.items()):
        for pair in INCOMPATIBLE_MULTIHOME:
            if pair.issubset(branches):
                findings.append(
                    _finding(
                        "KYM_STATEMENT_INCOMPATIBLE_MULTIHOME",
                        "warning",
                        "kym",
                        f"/statements/{statement_id}/tree_node_ids",
                        [statement_id],
                        "同一 statement 同时归入不相容语义分支，请拆分 statement 或选择主语义节点。",
                        {"branch_ids": sorted(branches), "incompatible_pair": sorted(pair)},
                        confidence="high",
                        current_node_ids=sorted(branches),
                        suggested_node_ids=sorted(branches - pair),
                    )
                )

    branch_refs: dict[str, set[str]] = {node_id: set() for node_id in FIXED_SECOND_LEVEL | {"target_profiles"}}
    for node_id, node in node_by_id.items():
        branch = _node_branch(node_id, parent_by_id)
        if branch in branch_refs:
            branch_refs[branch].update(node.get("statement_refs", []))

    for branch, statement_ids in sorted(branch_refs.items()):
        for statement_id in sorted(statement_ids):
            statement = statements.get(statement_id)
            if statement is None:
                continue
            text = statement["text"]
            path = f"/statements/{statement_id}"
            if (
                branch in {"customers", "users", "stakeholders"}
                and branch in statement["tree_node_ids"]
                and not ACTOR_RE.search(text)
            ):
                findings.append(
                    _finding(
                        "KYM_ROLE_BRANCH_TYPE_MISMATCH",
                        "warning",
                        "kym",
                        path,
                        [statement_id],
                        "Customer 角色分支应表达具体 actor 及其 Who/Why 关系。",
                        {"text": text, "role_branch": branch},
                        confidence="low",
                        resolution_required=False,
                        current_node_ids=[branch],
                        suggested_node_ids=["info", "business_rules", "team"],
                    )
                )
            if branch == "naming_conventions" and RULE_LIKE_RE.search(text):
                findings.append(
                    _finding(
                        "KYM_NAMING_RULE_LIKE_CONTENT",
                        "warning",
                        "kym",
                        path,
                        [statement_id],
                        "Naming Conventions 中出现规则型内容；去掉术语名后仍表达必须/不得/应保持等行为约束。",
                        {"text": text},
                        confidence="high",
                        current_node_ids=["naming_conventions"],
                        suggested_node_ids=["business_rules", "scope", "exclusions"],
                    )
                )
            if branch == "info" and (RULE_LIKE_RE.search(text) or statement["temporal_status"] == "future"):
                findings.append(
                    _finding(
                        "KYM_INFO_NORMATIVE_BEHAVIOR",
                        "warning",
                        "kym",
                        path,
                        [statement_id],
                        "Info 中出现规范性或未来行为内容；Info 应保存产品参考事实。",
                        {"text": text, "temporal_status": statement["temporal_status"]},
                        confidence="high",
                        current_node_ids=["info"],
                        suggested_node_ids=["business_rules", "scope"],
                    )
                )
            if branch == "scope" and NEGATIVE_RE.search(text):
                findings.append(
                    _finding(
                        "KYM_SCOPE_EXCLUSION_POLARITY",
                        "warning",
                        "kym",
                        path,
                        [statement_id],
                        "Scope 中出现排除或禁止极性，请确认是否应进入 Exclusions。",
                        {"text": text},
                        confidence="high",
                        current_node_ids=["scope"],
                        suggested_node_ids=["exclusions"],
                    )
                )
            if branch == "exclusions" and POSITIVE_RE.search(text) and not NEGATIVE_RE.search(text):
                findings.append(
                    _finding(
                        "KYM_SCOPE_EXCLUSION_POLARITY",
                        "warning",
                        "kym",
                        path,
                        [statement_id],
                        "Exclusions 中出现包含或支持极性，请确认是否应进入 Scope。",
                        {"text": text},
                        confidence="high",
                        current_node_ids=["exclusions"],
                        suggested_node_ids=["scope"],
                    )
                )
            if branch == "business_rules" and ARTIFACT_RE.search(text) and not RULE_LIKE_RE.search(text):
                findings.append(
                    _finding(
                        "KYM_BUSINESS_RULES_ARTIFACT_DUMP",
                        "warning",
                        "kym",
                        path,
                        [statement_id],
                        "Business Rules 像功能、工具或 artifact 清单，缺少条件/约束/行为语义。",
                        {"text": text},
                        confidence="high",
                        current_node_ids=["business_rules"],
                        suggested_node_ids=["deliverables", "equipments_and_tools", "documents"],
                    )
                )
            if branch == "documents" and (RULE_LIKE_RE.search(text) or CAPABILITY_RE.search(text) and not DOCUMENT_RE.search(text)):
                findings.append(
                    _finding(
                        "KYM_DOCUMENTS_TYPE_MISMATCH",
                        "warning",
                        "kym",
                        path,
                        [statement_id],
                        "Documents 应标识参考材料，不应承载材料中的规则或产品能力。",
                        {"text": text},
                        confidence="high",
                        current_node_ids=["documents"],
                        suggested_node_ids=["business_rules", "info"],
                    )
                )
            if (
                branch == "problems_unsolved"
                and statement["statement_type"] != "question"
                and "?" not in text
                and "？" not in text
                and not PROBLEM_RE.search(text)
            ):
                findings.append(
                    _finding(
                        "KYM_PROBLEMS_UNSOLVED_TYPE_MISMATCH",
                        "warning",
                        "kym",
                        path,
                        [statement_id],
                        "Problems Unsolved 应表达未决问题或待确认点。",
                        {"text": text},
                        confidence="low",
                        resolution_required=False,
                        current_node_ids=["problems_unsolved"],
                        suggested_node_ids=["scope", "exclusions", "schedule"],
                    )
                )
            if branch == "standards" and not STANDARD_RE.search(text):
                findings.append(
                    _finding(
                        "KYM_STANDARDS_TYPE_MISMATCH",
                        "warning",
                        "kym",
                        path,
                        [statement_id],
                        "Standards 应表达协议、行业标准、公司规范、schema 或格式约束。",
                        {"text": text},
                        confidence="low",
                        resolution_required=False,
                        current_node_ids=["standards"],
                        suggested_node_ids=["info", "documents"],
                    )
                )
            if branch == "deliverables" and CAPABILITY_RE.search(text) and not ARTIFACT_RE.search(text):
                findings.append(
                    _finding(
                        "KYM_DELIVERABLES_CAPABILITY_LEAK",
                        "warning",
                        "kym",
                        path,
                        [statement_id],
                        "Deliverables 应是具体工程 artifact；能力本身不是交付件清单。",
                        {"text": text},
                        confidence="high",
                        current_node_ids=["deliverables"],
                        suggested_node_ids=["business_rules", "scope"],
                    )
                )
            if branch == "equipments_and_tools" and RULE_LIKE_RE.search(text) and not ARTIFACT_RE.search(text):
                findings.append(
                    _finding(
                        "KYM_TOOLS_TYPE_MISMATCH",
                        "warning",
                        "kym",
                        path,
                        [statement_id],
                        "Equipments and Tools 应表达工程所需设备、工具或平台，不应承载产品规则。",
                        {"text": text},
                        confidence="high",
                        current_node_ids=["equipments_and_tools"],
                        suggested_node_ids=["business_rules"],
                    )
                )
            if branch == "schedule" and not TIME_RE.search(text):
                findings.append(
                    _finding(
                        "KYM_SCHEDULE_TIME_SEMANTICS_MISSING",
                        "warning",
                        "kym",
                        path,
                        [statement_id],
                        "Schedule 应包含时间、阶段、顺序、里程碑或持续更新语义。",
                        {"text": text},
                        confidence="low",
                        resolution_required=False,
                        current_node_ids=["schedule"],
                        suggested_node_ids=["team", "equipments_and_tools", "deliverables"],
                    )
                )
            if branch == "team" and RULE_LIKE_RE.search(text) and not TEAM_RE.search(text):
                findings.append(
                    _finding(
                        "KYM_TEAM_PRODUCT_RULE_LEAK",
                        "warning",
                        "kym",
                        path,
                        [statement_id],
                        "Team 应表达团队、角色或协作条件；这里像产品规则。",
                        {"text": text},
                        confidence="high",
                        current_node_ids=["team"],
                        suggested_node_ids=["business_rules"],
                    )
                )

    for statement_id, branches in sorted(statement_branches.items()):
        if {"scope", "exclusions"}.issubset(branches):
            findings.append(
                _finding(
                    "KYM_SCOPE_EXCLUSION_CONFLICT",
                    "error",
                    "kym",
                    f"/statements/{statement_id}/tree_node_ids",
                    [statement_id],
                    "同一 statement 同时进入 Scope 与 Exclusions，包含/排除边界冲突。",
                    {"branch_ids": sorted(branches)},
                    current_node_ids=["scope", "exclusions"],
                    suggested_node_ids=["scope", "exclusions"],
                )
            )
    return findings


def _audit_kym_tree_references(document: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    statements = document["statements"]
    statement_by_id = {item["id"]: item for item in statements}
    nodes = _walk_kym_tree(document["tree"])
    node_by_id = {item["id"]: item for item in nodes}
    for node in nodes:
        node_id = node["id"]
        for statement_id in sorted(set(node["statement_refs"])):
            statement = statement_by_id.get(statement_id)
            if statement is not None and node_id in statement["tree_node_ids"]:
                continue
            findings.append(
                _finding(
                    "KYM_TREE_REF_ASYMMETRY",
                    "error",
                    "kym",
                    f"/tree/{node_id}/statement_refs",
                    [node_id, statement_id],
                    "tree node 的 statement_refs 与 statement.tree_node_ids 不对称。",
                    {
                        "direction": "tree_to_statement",
                        "node_id": node_id,
                        "statement_id": statement_id,
                    },
                )
            )
    for statement in statements:
        statement_id = statement["id"]
        for node_id in sorted(set(statement["tree_node_ids"])):
            node = node_by_id.get(node_id)
            if node is not None and statement_id in node["statement_refs"]:
                continue
            findings.append(
                _finding(
                    "KYM_TREE_REF_ASYMMETRY",
                    "error",
                    "kym",
                    f"/statements/{statement_id}/tree_node_ids",
                    [statement_id, node_id],
                    "statement.tree_node_ids 与 tree node.statement_refs 不对称。",
                    {
                        "direction": "statement_to_tree",
                        "node_id": node_id,
                        "statement_id": statement_id,
                    },
                )
            )
    return findings


def _audit_kym_roles(document: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    node_by_id = {item["id"]: item for item in _walk_kym_tree(document["tree"])}
    role_ids = ("customers", "users", "stakeholders")
    actor_occurrences: dict[str, list[tuple[str, str]]] = {}
    statement_roles: dict[str, set[str]] = {}

    for role_id in role_ids:
        role = node_by_id.get(role_id)
        if role is None:
            continue
        for statement_id in _statement_refs(role):
            statement_roles.setdefault(statement_id, set()).add(role_id)
        for actor in role.get("children", []):
            actor_key = _normalize_text(actor["title"])
            if actor_key:
                actor_occurrences.setdefault(actor_key, []).append((role_id, actor["id"]))
            dimensions: dict[str, dict[str, Any]] = {}
            for child in actor.get("children", []):
                identity = f"{child['id']} {child['title']}".casefold()
                if re.search(r"(?:^|[-_\s])who(?:$|[-_\s])", identity):
                    dimensions["who"] = child
                if re.search(r"(?:^|[-_\s])why(?:$|[-_\s])", identity):
                    dimensions["why"] = child
            if not dimensions:
                continue
            for dimension in ("who", "why"):
                child = dimensions.get(dimension)
                if child is not None and _statement_refs(child):
                    continue
                findings.append(
                    _finding(
                        f"KYM_ROLE_{dimension.upper()}_GAP",
                        "warning",
                        "kym",
                        f"/tree/{role_id}/{actor['id']}",
                        [role_id, actor["id"]],
                        f"角色已建立 Who/Why 结构，但 {dimension.title()} 缺少来源支撑的陈述。",
                        {
                            "actor_id": actor["id"],
                            "dimension": dimension,
                            "role_id": role_id,
                        },
                    )
                )

    for actor_key, occurrences in sorted(actor_occurrences.items()):
        roles = sorted({role_id for role_id, _actor_id in occurrences})
        if len(roles) < 2:
            continue
        actor_ids = sorted(actor_id for _role_id, actor_id in occurrences)
        findings.append(
            _finding(
                "KYM_ROLE_DUPLICATE_PLACEMENT",
                "warning",
                "kym",
                "/tree/customer",
                actor_ids,
                "同名 actor 同时出现在多个 Customer 主角色分支，请确认主要关系归类。",
                {"actor_key": actor_key, "role_ids": roles},
            )
        )
    for statement_id, roles in sorted(statement_roles.items()):
        if len(roles) < 2:
            continue
        findings.append(
            _finding(
                "KYM_ROLE_DUPLICATE_PLACEMENT",
                "warning",
                "kym",
                f"/statements/{statement_id}/tree_node_ids",
                [statement_id],
                "同一陈述跨多个 Customer 主角色分支放置，请确认是否重复表达同一 actor。",
                {"role_ids": sorted(roles), "statement_id": statement_id},
            )
        )

    # ``authority`` 是来源 speaker/决策 authority，不是产品 actor role。
    # 这里只对有明确产品方向/治理语义的陈述给出高置信提示；普通
    # ``authority=User`` 的事实不会因为关键词相同而被误报。
    statements = _statement_by_id(document)
    for statement_id, roles in sorted(statement_roles.items()):
        statement = statements.get(statement_id)
        if statement is None or not PRODUCT_DIRECTION_RE.search(statement.get("text", "")):
            continue
        if not ({"customers", "users"} & roles):
            continue
        authority = str(statement.get("authority", "")).strip()
        # 只要 statement 已同时满足“产品方向/治理语义 + Customers/Users
        # 分支”这一确定性条件，就不能靠换一个具体职称或 authority 值绕过
        # Stakeholders 归位门禁。authority 仍保留在 finding 作为 provenance。
        confidence = "high"
        resolution_required = True
        findings.append(
            _finding(
                "KYM_SPEAKER_ACTOR_ROLE_CONFLICT",
                "warning",
                "kym",
                f"/statements/{statement_id}/tree_node_ids",
                [statement_id, *sorted(roles)],
                "对话 speaker/authority 不应自动充当产品 actor；产品方向决策或治理关系应归入 Stakeholders。",
                {
                    "authority": authority,
                    "role_ids": sorted(roles),
                    "text": statement.get("text", ""),
                    "reason": "product-direction/governance language under customer-or-user branch",
                },
                confidence=confidence,
                resolution_required=resolution_required,
                current_node_ids=sorted(roles),
                suggested_node_ids=["stakeholders"],
            )
        )
    return findings


def audit_kym_candidate(
    workspace: Path,
    product_id: str,
    candidate_path: Path,
    base_path: Path | None = None,
) -> dict[str, Any]:
    project_root(workspace, product_id)
    candidate = load_json(candidate_path)
    # Target Profile 的 minItems/minLength 形状由 candidate lint 解释为
    # 可定位 finding；其余 schema 错误仍保持既有 hard rejection。这样 CLI
    # 能报告“明确 Target 但 Profile 不完整”，而不是丢失到通用输入错误。
    candidate_schema_error: ContractError | None = None
    try:
        validate_document("kym", candidate, candidate_path)
    except ContractError as exc:
        if "target_profiles" not in str(exc):
            raise
        candidate_schema_error = exc
    _require_product(candidate, product_id, "KYM candidate")
    base: dict[str, Any] | None = None
    if base_path is not None:
        base = load_json(base_path)
        validate_document("kym", base, base_path)
        _require_product(base, product_id, "KYM base")

    findings = _audit_kym_tree_references(candidate)
    findings.extend(_audit_kym_roles(candidate))
    findings.extend(_audit_kym_semantic_placement(candidate))
    findings.extend(_audit_kym_target_profiles(candidate))
    findings.extend(_audit_kym_business_rules_shape(candidate))
    if candidate_schema_error is not None and not any(
        item["code"] == "KYM_TARGET_PROFILE_INCOMPLETE" for item in findings
    ):
        findings.append(
            _finding(
                "KYM_TARGET_PROFILE_INCOMPLETE",
                "error",
                "kym",
                "/target_profiles",
                ["target_profiles"],
                "Target Profile 不符合现有 Profile contract，无法作为完整候选使用。",
                {"schema_error": str(candidate_schema_error)},
                confidence="high",
                current_node_ids=["target_profiles"],
                suggested_node_ids=["target_profiles"],
            )
        )
    if base is not None:
        base_targets = {item["id"] for item in base["target_profiles"]}
        candidate_targets = {item["id"] for item in candidate["target_profiles"]}
        removed = sorted(base_targets - candidate_targets)
        if removed:
            findings.append(
                _finding(
                    "KYM_TARGET_PROFILE_REMOVED",
                    "error",
                    "kym",
                    "/target_profiles",
                    removed,
                    "候选 KYM 删除了 base 中的 Target Profile。",
                    {"removed_target_profile_ids": removed},
                )
            )
    return _report(
        report_kind="kym_candidate_semantic_risk",
        artifact_kind="kym",
        artifact_digest=digest_value(candidate),
        base_digest=digest_value(base) if base is not None else None,
        findings=findings,
    )


def _duplicate_values(values: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return sorted(duplicates)


SEMANTIC_EVIDENCE_REF_RE = re.compile(r"^[a-z][a-z0-9_-]+$")
RATIONALE_RE = re.compile(r"(?:^|_)(?:mapping|shared_evidence|evidence_mapping|traceability)_rationale$")
FAILURE_TAXONOMY_REQUIREMENT_RE = re.compile(
    r"(?:^|[-_\s])br[-_\s]?failure[-_\s]?taxonomy(?:$|[-_\s])|"
    r"failure[-_\s]?(?:taxonomy|classification|class|reason|root[-_\s]?cause)|"
    r"失败(?:分类|根因|原因)|根因分类",
    re.IGNORECASE,
)
FAILURE_CLASSIFIER_ROLE_RE = re.compile(
    r"failure[-_\s]?(?:taxonomy|classification|class|reason|root[-_\s]?cause)|"
    r"(?:provider[-_\s]?)?failure[-_\s]?classifier|"
    r"失败(?:分类|根因|原因)|根因分类",
    re.IGNORECASE,
)
FAILURE_CLASSIFIER_BAD_EVIDENCE_REFS = {
    "witness_candidate_artifact",
    "nf_multi_intent_candidate",
}
FAILURE_TAXONOMY_BASELINE_PATTERNS = {
    "grounding_failure": re.compile(r"grounding(?:[-_\s]?failure)?|grounding_failure", re.IGNORECASE),
    "extraction_failure": re.compile(r"extraction(?:[-_\s]?failure)?|extraction_failure", re.IGNORECASE),
    "overclaim": re.compile(r"over[-_\s]?claim|overclaim|过度声明", re.IGNORECASE),
    "lineage_failure": re.compile(r"lineage(?:[-_\s]?failure)?|lineage_failure", re.IGNORECASE),
    "execution_failure": re.compile(r"execution(?:[-_\s]?failure)?|execution_failure", re.IGNORECASE),
    "authority_failure": re.compile(r"authority(?:[-_\s]?(?:failure|violation))?|authority_failure", re.IGNORECASE),
}
FAILURE_CLASSIFIER_ROUTING_TOKENS = {
    "next_action",
    "prompt_adjustment",
    "source_scope_adjustment",
    "local_reprojection",
    "approved_live_rerun",
    "deterministic_pattern_candidate",
    "stop",
}
GENERATION_SYNTHESIS_CAPABILITY_RE = re.compile(
    r"candidate[-_\s]?(?:synthesis|materiali[sz]ation)|"
    r"(?:grounded[-_\s]?)?candidate[-_\s]?(?:generation|generator|synthesizer)|"
    r"(?:generate|produce|synthesi[sz]e|materiali[sz]e)[^\n;。]*candidate",
    re.IGNORECASE,
)
MATURITY_POSTURE_AUTHORITY_OUTPUT_RE = re.compile(
    r"(?:^|_)(?:"
    r"p05_review(?:_ready|_required|_queued)?|"
    r"review(?:_ready|_required|_queued|_routing|_posture)?|"
    r"diagnostic(?:_only)?|no_candidate|not_review_ready|"
    r"candidate_hypothesis_rejected|"
    r"p06(?:_|$)|eligible(?:_for_formalization)?|ineligible|"
    r"approved|approval|formal(?:_m1|ization|izer)?|"
    r"canonical(?:_truth|_write)?"
    r")(?:$|_)",
    re.IGNORECASE,
)
COMBINED_RESPONSIBILITY_RATIONALE_RE = re.compile(
    r"combined[-_\s]?responsibilit(?:y|ies)[-_\s]?rationale|"
    r"explicit[-_\s]?combined[-_\s]?responsibilit(?:y|ies)",
    re.IGNORECASE,
)
COMBINED_RESPONSIBILITY_BASIS_RE = re.compile(
    r"indivisible[-_\s]?contract|不可分割合同依据|明确复合职责依据",
    re.IGNORECASE,
)
NEGATED_RATIONALE_RE = re.compile(
    r"(?:without|no|missing|lacks?|not|缺少|没有|无)[^\n;。]{0,48}"
    r"(?:combined[-_\s]?responsibilit|indivisible[-_\s]?contract|不可分割合同依据|明确复合职责依据)|"
    r"(?:combined[-_\s]?responsibilit|indivisible[-_\s]?contract|不可分割合同依据|明确复合职责依据)"
    r"[^\n;。]{0,48}(?:absent|missing|lacking|缺失|缺少|没有|无)",
    re.IGNORECASE,
)
CANDIDATE_READINESS_EVALUATION_RE = re.compile(
    r"candidate[-_\s]?(?:review[-_\s]?)?readiness|"
    r"review[-_\s]?readiness|"
    r"p05[-_\s]?review[-_\s]?ready|"
    r"review[-_\s]?ready",
    re.IGNORECASE,
)
READINESS_BOUNDARY_INVARIANT_RE = re.compile(
    r"review[-_\s]?ready\s*(?:!=|is\s+not|not\s+equal\s+to|不是|不等于)"
    r"[^\n;。]*p06[^\n;。]*(?:!=|is\s+not|not\s+equal\s+to|不是|不等于)[^\n;。]*formal|"
    r"review[-_\s]?ready[^\n;。]*(?:not\s+p06|不是\s*p06)[^\n;。]*(?:not\s+formal|不是\s*formal)",
    re.IGNORECASE,
)
CANDIDATE_GENERATION_OUTPUT_RE = re.compile(
    r"(?:^|_)candidate_(?:hypothesis|artifact|proposal|package)(?:$|_)|"
    r"(?:^|_)grounded_candidate(?:$|_)",
    re.IGNORECASE,
)
LANE_AUTHORITY_REQUIREMENT_RE = re.compile(
    r"(?:^|[-_\s])(?:req|br)[-_\s]?(?:p06[-_\s]?)?lane[-_\s]?relationship(?:$|[-_\s])|"
    r"lane[-_\s]?(?:relationship|authority|separation)|"
    r"truth[-_\s]?(?:boundary|boundaries|domain)|"
    r"authority[-_\s]?(?:boundary|separation|domain)|"
    r"semantic[-_\s]?authority|"
    r"canonical[-_\s]?truth|"
    r"provider[^\n;。]{0,80}deterministic[^\n;。]{0,40}lane",
    re.IGNORECASE,
)
LANE_AUTHORITY_CAPABILITY_RE = re.compile(
    r"preserve[-_\s]?truth|"
    r"truth[-_\s]?(?:boundary|boundaries|domain)|"
    r"authority[-_\s]?(?:boundary|separation|domain)|"
    r"semantic[-_\s]?authority|"
    r"canonical[-_\s]?truth|"
    r"lane[-_\s]?(?:authority|separation)",
    re.IGNORECASE,
)
CANDIDATE_EVIDENCE_DOMAIN_RE = re.compile(
    r"(?:^|_)(?:p05_)?candidate(?:_|$)|"
    r"multi_intent_candidate|"
    r"candidate_(?:artifact|bundle|proposal|content|shape|quality)",
    re.IGNORECASE,
)
CANDIDATE_OWNER_DOMAIN_RE = re.compile(
    r"p05[-_\s]?candidate|"
    r"candidate[-_\s]?(?:owner|validator|review|readiness|content|quality)",
    re.IGNORECASE,
)
CANDIDATE_CLAIM_CEILING_RE = re.compile(
    r"candidate[-_\s]?(?:proposal|artifact|content|quality|shape)?[-_\s]?only|"
    r"p05[-_\s]?candidate[-_\s]?only",
    re.IGNORECASE,
)
TRUTH_DOMAIN_WITNESS_RE = re.compile(r"truth[-_\s]?domain|claim[-_\s]?ceiling", re.IGNORECASE)
AUTHORITY_BOUNDARY_NF_RE = re.compile(
    r"provider[-_\s]?as[-_\s]?canonical|"
    r"runtime[-_\s]?success[-_\s]?as[-_\s]?semantic|"
    r"semantic[-_\s]?authority|"
    r"canonical[-_\s]?truth",
    re.IGNORECASE,
)
CONFIGURATION_EVOLUTION_REQUIREMENT_RE = re.compile(
    r"(?:provider[-_\s]?)?configuration[-_\s]?(?:version|evolution|lineage|variant|supersession|comparability)|"
    r"(?:same[-_\s]?configuration|configuration[-_\s]?variant|non[-_\s]?comparable|comparability[-_\s]?unknown)|"
    r"(?:prompt|model|schema|provider)[^\n;。]{0,64}(?:version|migration|variant|supersed|alias|comparab|lineage)",
    re.IGNORECASE,
)
CONFIGURATION_VERSION_CAPABILITY_RE = re.compile(
    r"m1[-_\s]?provider[-_\s]?configuration[-_\s]?version[-_\s]?management|"
    r"(?:provider[-_\s]?)?configuration[-_\s]?(?:version[-_\s]?management|evolution|lineage|comparability|variant)",
    re.IGNORECASE,
)
CONFIGURATION_VERSION_CONTRACT_PATTERNS = {
    "configuration_identity": re.compile(
        r"configuration[-_\s]?identity|provider[-_\s]?configuration[-_\s]?(?:id|digest)|configuration[-_\s]?digest",
        re.IGNORECASE,
    ),
    "change_dimensions": re.compile(
        r"change[-_\s]?dimensions|prompt[^\n;。]{0,24}(?:version|change)|model[^\n;。]{0,24}(?:alias|version|change)|schema[^\n;。]{0,24}(?:version|change)|provider[^\n;。]{0,24}(?:version|change)",
        re.IGNORECASE,
    ),
    "parent_supersedes_relation": re.compile(
        r"parent|variant|supersed|supersession|lineage",
        re.IGNORECASE,
    ),
    "comparison_eligibility": re.compile(
        r"comparison[-_\s]?eligibility|comparability|same_configuration|configuration_variant|non_comparable|comparability_unknown",
        re.IGNORECASE,
    ),
    "unknown_or_non_comparable_posture": re.compile(
        r"non[-_\s]?comparable|non_comparable|comparability[-_\s]?unknown|comparability_unknown|unknown",
        re.IGNORECASE,
    ),
}
CONFIGURATION_IDENTITY_KEYS = (
    "provider_configuration_id",
    "configuration_id",
    "configuration_digest",
    "configuration_identity_digest",
)
CONFIGURATION_DIMENSION_KEYS = (
    "provider_id",
    "provider",
    "model_alias",
    "observed_model_alias",
    "prompt_id",
    "prompt_version",
    "schema_id",
    "schema_version",
)
COMPARABILITY_VERDICT_KEYS = (
    "comparability_verdict",
    "configuration_comparability",
    "comparison_verdict",
)
COMPARABILITY_ELIGIBILITY_KEYS = (
    "comparison_eligibility",
    "comparability_eligibility",
)
COMPARABILITY_VERDICTS = {
    "same_configuration",
    "configuration_variant",
    "non_comparable",
    "comparability_unknown",
}
NON_COMPARABLE_VERDICTS = {"non_comparable", "comparability_unknown"}
COMPARISON_ELIGIBLE_VALUES = {"eligible", "comparison_eligible", "comparable", "true"}
IMMUTABLE_MODEL_RESOLUTION_KEYS = {
    "immutable_model_id",
    "model_digest",
    "model_snapshot_id",
    "model_resolution_evidence_ref",
    "resolved_model_digest",
}
SAME_MODEL_CLAIM_RE = re.compile(
    r"same[-_\s]?model|same[-_\s]?configuration|reproduc(?:e|ible|ibility)|同一模型|同配置复现",
    re.IGNORECASE,
)
CLAIM_BOUNDARY_EXTERNAL_SOURCE_REF_RE = re.compile(
    r"^(?:review|finding|pdi|goal|non[-_]?goal|authority|truth|target[-_]?exclusion|"
    r"source[-_]?contract|kym[-_]?target)[-_.]",
    re.IGNORECASE,
)
CLAIM_BOUNDARY_RISK_PATTERNS = {
    "semantic_risk": re.compile(
        r"semantic[-_\s]?(?:risk|correctness|validation|authority|truth)|"
        r"truth[-_\s]?(?:domain|boundary)|source[-_\s]?grounded[-_\s]?evidence",
        re.IGNORECASE,
    ),
    "provider_generation_or_assessment": re.compile(
        r"provider(?:[-_\s]?backed)?[^\n;。]{0,80}"
        r"(?:generation|assessment|evaluation|call|accountability|lane)|"
        r"(?:generation|assessment|evaluation)[^\n;。]{0,80}provider",
        re.IGNORECASE,
    ),
    "candidate_generation_or_readiness": re.compile(
        r"candidate[^\n;。]{0,80}(?:generation|materiali[sz]ation|readiness|review|hypothesis)|"
        r"(?:generation|materiali[sz]ation|readiness|review)[^\n;。]{0,80}candidate",
        re.IGNORECASE,
    ),
    "authority_or_p06_adjacent": re.compile(
        r"authority|p06|disposition|eligible(?:_for_formalization)?|formal(?:ization|izer|[-_\s]?m1)?|approval",
        re.IGNORECASE,
    ),
    "canonical_or_test_store_write": re.compile(
        r"canonical|test[-_\s]?store|(?:commit|write|store)[^\n;。]{0,40}(?:truth|model|artifact)",
        re.IGNORECASE,
    ),
    "runtime_to_semantic_boundary": re.compile(
        r"(?:runtime|execution)[^\n;。]{0,80}(?:semantic|truth|authority)|"
        r"(?:semantic|truth|authority)[^\n;。]{0,80}(?:runtime|execution)",
        re.IGNORECASE,
    ),
    "comparison_or_arena": re.compile(
        r"compare|comparison|arena|semantic[-_\s]?winner|"
        r"provider[^\n;。]{0,80}deterministic|deterministic[^\n;。]{0,80}provider",
        re.IGNORECASE,
    ),
    "promotion": re.compile(
        r"promotion|promote|automatic[-_\s]?promotion|maintained[-_\s]?support",
        re.IGNORECASE,
    ),
}
MC_LIFECYCLE_CATEGORY_PATTERNS = {
    "evidence_production": re.compile(
        r"observation[-_\s]?envelope|provider[-_\s]?trial[-_\s]?expansion|trial[-_\s]?evidence|"
        r"language[-_\s]?(?:and|/)?[-_\s]?source[-_\s]?shape|source[-_\s]?shape[-_\s]?matrix|breadth[-_\s]?matrix|profile[-_\s]?evidence",
        re.IGNORECASE,
    ),
    "comparative_evaluation": re.compile(
        r"comparative[-_\s]?evaluation|provider[-_\s]?(?:vs|versus|/)[-_\s]?deterministic|"
        r"deterministic[-_\s]?(?:vs|versus|/)[-_\s]?provider|arena|calibration[-_\s]?evidence",
        re.IGNORECASE,
    ),
    "maintained_promotion": re.compile(
        r"maintained[-_\s]?(?:capability[-_\s]?)?promotion|maintained[-_\s]?support|"
        r"promotion[-_\s]?gate|promotion[-_\s]?owner|maintained[-_\s]?authority|replay[-_\s]?(?:gate|evidence)",
        re.IGNORECASE,
    ),
}
MC_GROUPING_RATIONALE_RE = re.compile(r"grouping[-_\s]?rationale|分组理由", re.IGNORECASE)
MC_GROUPING_RATIONALE_PARTS = {
    "lifecycle": re.compile(r"lifecycle|生命周期", re.IGNORECASE),
    "owner": re.compile(r"owner|authority|负责人|权威", re.IGNORECASE),
    "acceptance_ladder": re.compile(r"acceptance[-_\s]?ladder|验收", re.IGNORECASE),
    "single_goal_closeout": re.compile(r"single[-_\s]?goal|same[-_\s]?goal|同一\s*goal|可关闭", re.IGNORECASE),
}
VISION_PROFILE_RE = re.compile(
    r"non[-_\s]?executable[-_\s]?vision[-_\s]?profile|"
    r"(?:long[-_\s]?term|family|vision|产品族|长期)[^\n;。]{0,80}(?:profile|target|mainline)|"
    r"target[-_\s]?provider[-_\s]?backed[-_\s]?java[-_\s]?python[-_\s]?mainline",
    re.IGNORECASE,
)
NON_EXECUTABLE_VISION_RATIONALE_RE = re.compile(
    r"non[-_\s]?executable[-_\s]?vision[-_\s]?profile|"
    r"(?:not|never|cannot|不得|不能|不可)[^\n;。]{0,80}(?:single[-_\s]?)?(?:executable|可执行|closeout|收口)|"
    r"(?:vision|family|产品族|长期)[^\n;。]{0,80}(?:proposal[-_\s]?only|not[-_\s]?approved|claim[-_\s]?ceiling|非可执行)",
    re.IGNORECASE,
)


def _normal_signature(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _normal_signature(item)
            for key, item in sorted(value.items())
            if not RATIONALE_RE.search(str(key))
        }
    if isinstance(value, list):
        return sorted(
            (_normal_signature(item) for item in value),
            key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
        )
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    return value


def _has_mapping_rationale(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            (RATIONALE_RE.search(str(key)) and bool(item))
            or _has_mapping_rationale(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_has_mapping_rationale(item) for item in value)
    if isinstance(value, str):
        return bool(RATIONALE_RE.search(value))
    return False


def _flatten_strings(value: Any) -> list[str]:
    strings: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for child in item.values():
                visit(child)
            return
        if isinstance(item, list):
            for child in item:
                visit(child)
            return
        if item is None:
            return
        strings.append(str(item))

    visit(value)
    return strings


def _joined_text(value: Any) -> str:
    return " ".join(item for item in _flatten_strings(value) if item).casefold()


def _semantic_text_for_node(node: dict[str, Any]) -> str:
    fields = [
        node.get("id", ""),
        node.get("title", ""),
        node.get("topic", ""),
        node.get("scope", []),
        node.get("purpose", ""),
        node.get("source_statement_refs", []),
        node.get("capability_type", ""),
        node.get("inputs", []),
        node.get("outputs", []),
        node.get("decision_semantics", {}),
        node.get("metadata_requirements", []),
        node.get("coverage_requirements", []),
        node.get("excluded_outputs", []),
        node.get("claim_exclusions", []),
        node.get("claim_exclusions_not_applicable", {}),
        node.get("invariants", []),
        node.get("witness_artifact_refs", []),
        node.get("negative_fixture_refs", []),
        node.get("traceability_refs", []),
    ]
    return _joined_text(fields)


def _semantic_text_for_requirement(requirement: dict[str, Any]) -> str:
    fields = [
        requirement.get("id", ""),
        requirement.get("source_requirement_ref", ""),
        requirement.get("title", ""),
        requirement.get("validation_owner", ""),
        requirement.get("witness_evidence_mapping", []),
        requirement.get("nf_evidence_mapping", []),
        requirement.get("claim_ceiling", ""),
        requirement.get("missing_items", []),
        requirement.get("required_coverage_requirement_ids", []),
        requirement.get("required_scope", []),
        requirement.get("acceptance_predicates", []),
    ]
    return _joined_text(fields)


def _mc_lifecycle_categories(node: dict[str, Any]) -> list[str]:
    text = _semantic_text_for_node(node)
    return sorted(
        category
        for category, pattern in MC_LIFECYCLE_CATEGORY_PATTERNS.items()
        if pattern.search(text)
    )


def _mc_grouping_rationale_gaps(node: dict[str, Any]) -> list[str]:
    text = _joined_text(
        [
            node.get("id", ""),
            node.get("title", ""),
            node.get("topic", ""),
            node.get("scope", {}),
            node.get("purpose", ""),
            node.get("source_statement_refs", []),
            node.get("traceability_refs", []),
        ]
    )
    if MC_GROUPING_RATIONALE_RE.search(text) is None:
        return ["grouping_rationale"]
    return sorted(
        name
        for name, pattern in MC_GROUPING_RATIONALE_PARTS.items()
        if pattern.search(text) is None
    )


def _profile_text(profile: dict[str, Any]) -> str:
    return _joined_text(
        [
            profile.get("id", ""),
            profile.get("label", ""),
            profile.get("source_target_id", ""),
            profile.get("description", ""),
            profile.get("claim_ceiling", ""),
        ]
    )


def _audit_tco_mc_lifecycle_acceptance_coherence(
    model: dict[str, Any],
    *,
    code_prefix: str,
) -> list[Finding]:
    findings: list[Finding] = []
    nodes_by_id = {node["id"]: node for node in model["nodes"]}
    required_categories = set(MC_LIFECYCLE_CATEGORY_PATTERNS)
    for node in model.get("nodes", []):
        if node.get("node_kind") != "mc":
            continue
        child_capabilities = [
            nodes_by_id[child_id]
            for child_id in node.get("children", [])
            if child_id in nodes_by_id and nodes_by_id[child_id].get("node_kind") in {"m1", "capability", "policy"}
        ]
        if len(child_capabilities) < 2:
            continue
        categories_by_child = {
            child["id"]: _mc_lifecycle_categories(child)
            for child in child_capabilities
        }
        present = {
            category
            for categories in categories_by_child.values()
            for category in categories
        }
        if not required_categories <= present:
            continue
        rationale_gaps = _mc_grouping_rationale_gaps(node)
        if not rationale_gaps:
            continue
        authority_owners = sorted({
            str(child.get("decision_semantics", {}).get("authority_owner", "")).strip()
            for child in child_capabilities
            if str(child.get("decision_semantics", {}).get("authority_owner", "")).strip()
        })
        findings.append(
            _finding(
                f"{code_prefix}_MC_LIFECYCLE_ACCEPTANCE_INCOHERENT",
                "error",
                "tco_model",
                f"/nodes/{node['id']}/children",
                [node["id"], *[child["id"] for child in child_capabilities]],
                "MC 同时包含 evidence production、comparative evaluation 与 maintained promotion 生命周期时，必须拆分为 Provider Trial Expansion / Comparative Evaluation / Maintained Capability Promotion，或提供覆盖 lifecycle、owner、acceptance ladder 与 single-Goal closeout 的 grouping_rationale。",
                {
                    "categories_by_child": categories_by_child,
                    "rationale_gaps": rationale_gaps,
                    "authority_owners": authority_owners,
                },
                current_node_ids=[node["id"]],
                suggested_node_ids=[
                    "mc-provider-trial-expansion",
                    "mc-comparative-evaluation",
                    "mc-maintained-capability-promotion",
                ],
            )
        )
    for profile_index, profile in enumerate(model.get("target_profiles", [])):
        text = _profile_text(profile)
        if VISION_PROFILE_RE.search(text) is None or NON_EXECUTABLE_VISION_RATIONALE_RE.search(text):
            continue
        findings.append(
            _finding(
                f"{code_prefix}_VISION_PROFILE_EXECUTABLE_CLOSEOUT_RISK",
                "error",
                "tco_model",
                f"/target_profiles/{profile_index}",
                [str(profile.get("id", "")), str(profile.get("source_target_id", ""))],
                "长期 family/vision profile 不能被当作单一 executable closeout profile；请用 non_executable_vision_profile 或等价 claim ceiling/rationale 标记为 proposal/vision，不得改变历史 Target denominator 或 approval。",
                {
                    "label": profile.get("label", ""),
                    "description": profile.get("description", ""),
                    "claim_ceiling": profile.get("claim_ceiling", ""),
                },
                suggested_node_ids=["non_executable_vision_profile"],
            )
        )
    return findings


def _requires_failure_taxonomy(requirement: dict[str, Any]) -> bool:
    return bool(FAILURE_TAXONOMY_REQUIREMENT_RE.search(_semantic_text_for_requirement(requirement)))


def _failure_taxonomy_classes(text: str) -> list[str]:
    return sorted(
        class_id
        for class_id, pattern in FAILURE_TAXONOMY_BASELINE_PATTERNS.items()
        if pattern.search(text)
    )


def _supports_failure_classifier(node: dict[str, Any]) -> bool:
    text = _semantic_text_for_node(node)
    has_role = bool(FAILURE_CLASSIFIER_ROLE_RE.search(text))
    classes = _failure_taxonomy_classes(text)
    return has_role and len(classes) >= 3


def _failure_classifier_bad_evidence_refs(
    requirement: dict[str, Any], mapped_capabilities: list[dict[str, Any]]
) -> list[str]:
    refs = {
        *_semantic_mapping_refs(requirement.get("witness_evidence_mapping", []), ("witness",)),
        *_semantic_mapping_refs(requirement.get("nf_evidence_mapping", []), ("nf", "negative")),
    }
    for node in mapped_capabilities:
        refs.update(str(item) for item in node.get("witness_artifact_refs", []))
        refs.update(str(item) for item in node.get("negative_fixture_refs", []))
    return sorted(ref for ref in refs if _canonical_token(ref) in FAILURE_CLASSIFIER_BAD_EVIDENCE_REFS)


def _failure_classifier_routing_tokens(node: dict[str, Any]) -> list[str]:
    semantic = node.get("decision_semantics", {})
    values = [
        node.get("outputs", []),
        semantic.get("decision_outputs", []),
        semantic.get("terminal_outcomes", []),
        semantic.get("failure_outcomes", []),
    ]
    tokens = {_canonical_token(item) for item in _flatten_strings(values)}
    return sorted(tokens & FAILURE_CLASSIFIER_ROUTING_TOKENS)


def _node_role_text(node: dict[str, Any]) -> str:
    return _joined_text(
        [
            node.get("id", ""),
            node.get("title", ""),
            node.get("topic", ""),
            node.get("capability_type", ""),
        ]
    )


def _declared_output_tokens(node: dict[str, Any]) -> list[str]:
    semantic = node.get("decision_semantics", {})
    fields = [
        node.get("outputs", []),
        semantic.get("decision_outputs", []),
        semantic.get("decision_output", []),
        semantic.get("terminal_outcomes", []),
        semantic.get("failure_outcomes", []),
        semantic.get("failure_modes", []),
    ]
    return sorted({_canonical_token(item) for item in _flatten_strings(fields) if str(item).strip()})


def _maturity_posture_authority_outputs(node: dict[str, Any]) -> list[str]:
    return sorted(
        token
        for token in _declared_output_tokens(node)
        if MATURITY_POSTURE_AUTHORITY_OUTPUT_RE.search(token)
    )


def _candidate_generation_outputs(node: dict[str, Any]) -> list[str]:
    return sorted(
        token
        for token in _declared_output_tokens(node)
        if CANDIDATE_GENERATION_OUTPUT_RE.search(token)
    )


def _requires_lane_authority(requirement: dict[str, Any]) -> bool:
    text = _joined_text(
        [
            requirement.get("id", ""),
            requirement.get("source_requirement_ref", ""),
            requirement.get("title", ""),
            requirement.get("claim_ceiling", ""),
            requirement.get("required_scope", []),
            requirement.get("acceptance_predicates", []),
        ]
    )
    return bool(LANE_AUTHORITY_REQUIREMENT_RE.search(text))


def _supports_lane_authority_capability(node: dict[str, Any]) -> bool:
    semantic = node.get("decision_semantics", {})
    text = _joined_text(
        [
            node.get("id", ""),
            node.get("title", ""),
            node.get("topic", ""),
            node.get("capability_type", ""),
            node.get("purpose", ""),
            node.get("scope", {}),
            semantic.get("decision_rules", []),
            node.get("invariants", []),
        ]
    )
    return bool(LANE_AUTHORITY_CAPABILITY_RE.search(text))


def _lane_authority_candidate_domain_refs(
    requirement: dict[str, Any],
    mapped_capabilities: list[dict[str, Any]],
) -> list[str]:
    refs = {
        *_semantic_mapping_refs(requirement.get("witness_evidence_mapping", []), ("witness",)),
        *_semantic_mapping_refs(requirement.get("nf_evidence_mapping", []), ("nf", "negative")),
    }
    for node in mapped_capabilities:
        refs.update(str(item) for item in node.get("witness_artifact_refs", []))
        refs.update(str(item) for item in node.get("negative_fixture_refs", []))
    return sorted(ref for ref in refs if CANDIDATE_EVIDENCE_DOMAIN_RE.search(_canonical_token(ref)))


def _lane_authority_domain_evidence_gaps(
    requirement: dict[str, Any],
    mapped_capabilities: list[dict[str, Any]],
) -> list[str]:
    witness_refs = {
        *_semantic_mapping_refs(requirement.get("witness_evidence_mapping", []), ("witness",)),
        *(str(item) for node in mapped_capabilities for item in node.get("witness_artifact_refs", [])),
    }
    nf_refs = {
        *_semantic_mapping_refs(requirement.get("nf_evidence_mapping", []), ("nf", "negative")),
        *(str(item) for node in mapped_capabilities for item in node.get("negative_fixture_refs", [])),
    }
    missing: list[str] = []
    if not any(TRUTH_DOMAIN_WITNESS_RE.search(_canonical_token(ref)) for ref in witness_refs):
        missing.append("truth_domain_or_claim_ceiling_witness")
    if not any(AUTHORITY_BOUNDARY_NF_RE.search(_canonical_token(ref)) for ref in nf_refs):
        missing.append("provider_runtime_authority_boundary_nf")
    return missing


def _requires_provider_configuration_version_management(requirement: dict[str, Any]) -> bool:
    return bool(CONFIGURATION_EVOLUTION_REQUIREMENT_RE.search(_semantic_text_for_requirement(requirement)))


def _configuration_version_contract_gaps(node: dict[str, Any]) -> list[str]:
    text = _semantic_text_for_node(node)
    missing = [
        name
        for name, pattern in CONFIGURATION_VERSION_CONTRACT_PATTERNS.items()
        if pattern.search(text) is None
    ]
    if not node.get("witness_artifact_refs"):
        missing.append("witness")
    if not node.get("negative_fixture_refs"):
        missing.append("negative_fixtures")
    return sorted(set(missing))


def _is_configuration_version_capability(node: dict[str, Any]) -> bool:
    text = _joined_text(
        [
            node.get("id", ""),
            node.get("title", ""),
            node.get("topic", ""),
            node.get("capability_type", ""),
            node.get("purpose", ""),
            node.get("outputs", []),
            node.get("decision_semantics", {}),
            node.get("invariants", []),
            node.get("witness_artifact_refs", []),
            node.get("negative_fixture_refs", []),
        ]
    )
    return CONFIGURATION_VERSION_CAPABILITY_RE.search(text) is not None


def _supports_configuration_version_management(node: dict[str, Any]) -> bool:
    return _is_configuration_version_capability(node) and not _configuration_version_contract_gaps(node)


def _audit_tco_provider_configuration_version_management(
    model: dict[str, Any],
    *,
    code_prefix: str,
) -> list[Finding]:
    findings: list[Finding] = []
    nodes_by_id = {node["id"]: node for node in model["nodes"]}

    for node in model.get("nodes", []):
        if node.get("node_kind") != "m1" or not _is_configuration_version_capability(node):
            continue
        node_id = str(node.get("id", ""))
        missing = _configuration_version_contract_gaps(node)
        if not missing:
            continue
        findings.append(
            _finding(
                f"{code_prefix}_PROVIDER_CONFIGURATION_VERSION_M1_CONTRACT_INCOMPLETE",
                "error",
                "tco_model",
                f"/nodes/{node_id}",
                [node_id, *missing],
                "provider configuration version management M1 必须分别声明 configuration identity、change dimensions、parent/supersedes relation、comparison eligibility、unknown/non-comparable posture、witness 与负例。",
                {
                    "missing_contract_parts": missing,
                    "capability_type": node.get("capability_type", ""),
                    "witness_artifact_refs": node.get("witness_artifact_refs", []),
                    "negative_fixture_refs": node.get("negative_fixture_refs", []),
                },
                current_node_ids=[node_id],
                suggested_node_ids=["m1-provider-configuration-version-management"],
            )
        )

    for profile_index, profile in enumerate(model["target_profiles"]):
        for req_index, requirement in enumerate(profile["requirements"]):
            if not _requires_provider_configuration_version_management(requirement):
                continue
            path = f"/target_profiles/{profile_index}/requirements/{req_index}"
            req_id = str(requirement["id"])
            mapped_nodes = [nodes_by_id[node_id] for node_id in requirement["node_ids"] if node_id in nodes_by_id]
            mapped_capabilities = [node for node in mapped_nodes if node.get("node_kind") in {"m1", "capability"}]
            compatible_nodes = [
                node for node in mapped_capabilities if _supports_configuration_version_management(node)
            ]
            if compatible_nodes:
                continue
            findings.append(
                _finding(
                    f"{code_prefix}_REQUIREMENT_PROVIDER_CONFIGURATION_VERSION_MISMATCH",
                    "error",
                    "tco_model",
                    f"{path}/node_ids",
                    [profile["id"], req_id, *requirement["node_ids"]],
                    "Target/KYM requirement 要求跨配置 trial、版本迁移、supersession 或比较时，不能映射到普通 Provider/model/prompt/schema identity freeze M1 后宣称覆盖；必须映射独立 configuration version management M1 或暴露 candidate gap M1。",
                    {
                        "source_requirement_ref": requirement.get("source_requirement_ref", ""),
                        "requirement_title": requirement.get("title", ""),
                        "mapped_m1_summary": _mapped_capability_summary(mapped_capabilities),
                    },
                    current_node_ids=requirement["node_ids"],
                    suggested_node_ids=["m1-provider-configuration-version-management"],
                )
            )
    return findings


def _mapped_capability_summary(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": node["id"],
            "title": node.get("title", ""),
            "capability_type": node.get("capability_type", ""),
            "purpose": node.get("purpose", ""),
            "authority_owner": node.get("decision_semantics", {}).get("authority_owner", ""),
            "source_statement_refs": node.get("source_statement_refs", []),
            "witness_artifact_refs": node.get("witness_artifact_refs", []),
            "negative_fixture_refs": node.get("negative_fixture_refs", []),
        }
        for node in nodes
    ]


def _mapped_acceptance_predicates(model: dict[str, Any], node_id: str) -> list[str]:
    predicates: list[str] = []
    for profile in model.get("target_profiles", []):
        for requirement in profile.get("requirements", []):
            if node_id not in requirement.get("node_ids", []):
                continue
            predicates.extend(str(item) for item in requirement.get("acceptance_predicates", []))
    return predicates


def _has_combined_responsibility_rationale(node: dict[str, Any]) -> bool:
    text = _semantic_text_for_node(node)
    return (
        COMBINED_RESPONSIBILITY_RATIONALE_RE.search(text) is not None
        and COMBINED_RESPONSIBILITY_BASIS_RE.search(text) is not None
        and NEGATED_RATIONALE_RE.search(text) is None
    )


def _claim_boundary_risk_reasons(node: dict[str, Any]) -> list[str]:
    if node.get("node_kind") != "m1":
        return []
    text = _joined_text(
        [
            node.get("id", ""),
            node.get("title", ""),
            node.get("topic", ""),
            node.get("scope", {}),
            node.get("purpose", ""),
            node.get("source_statement_refs", []),
            node.get("capability_type", ""),
            node.get("inputs", []),
            node.get("outputs", []),
            node.get("decision_semantics", {}),
            node.get("coverage_requirements", []),
            node.get("invariants", []),
            node.get("witness_artifact_refs", []),
            node.get("negative_fixture_refs", []),
            node.get("traceability_refs", []),
        ]
    )
    return sorted(
        risk_id
        for risk_id, pattern in CLAIM_BOUNDARY_RISK_PATTERNS.items()
        if pattern.search(text)
    )


def _claim_boundary_source_refs(node: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for item in node.get("claim_exclusions", []):
        if isinstance(item, dict):
            refs.extend(str(ref) for ref in item.get("source_refs", []))
    not_applicable = node.get("claim_exclusions_not_applicable")
    if isinstance(not_applicable, dict):
        refs.extend(str(ref) for ref in not_applicable.get("source_refs", []))
    return sorted(set(refs))


def _mapped_requirement_refs_for_node(model: dict[str, Any], node_id: str) -> set[str]:
    refs: set[str] = set()
    for profile in model.get("target_profiles", []):
        refs.add(str(profile.get("id", "")))
        refs.add(str(profile.get("source_target_id", "")))
        for requirement in profile.get("requirements", []):
            if node_id not in requirement.get("node_ids", []):
                continue
            refs.add(str(requirement.get("id", "")))
            refs.add(str(requirement.get("source_requirement_ref", "")))
    return {ref for ref in refs if ref}


def _unbound_claim_boundary_source_refs(model: dict[str, Any], node: dict[str, Any]) -> list[str]:
    bound_refs = {
        str(ref)
        for ref in [
            *node.get("source_statement_refs", []),
            *node.get("traceability_refs", []),
        ]
        if str(ref)
    }
    node_id = str(node.get("id", ""))
    bound_refs.update(_mapped_requirement_refs_for_node(model, node_id))
    return sorted(
        ref
        for ref in _claim_boundary_source_refs(node)
        if ref not in bound_refs and CLAIM_BOUNDARY_EXTERNAL_SOURCE_REF_RE.search(ref) is None
    )


def _audit_tco_claim_exclusion_boundaries(
    model: dict[str, Any],
    *,
    code_prefix: str,
) -> list[Finding]:
    findings: list[Finding] = []
    for node in model.get("nodes", []):
        node_id = str(node.get("id", ""))
        risk_reasons = _claim_boundary_risk_reasons(node)
        has_claim_exclusions = bool(node.get("claim_exclusions"))
        has_not_applicable = bool(node.get("claim_exclusions_not_applicable"))
        if has_claim_exclusions or has_not_applicable:
            unbound_refs = _unbound_claim_boundary_source_refs(model, node)
            if unbound_refs:
                findings.append(
                    _finding(
                        f"{code_prefix}_M1_CLAIM_EXCLUSION_SOURCE_REF_UNBOUND",
                        "error",
                        "tco_model",
                        f"/nodes/{node_id}/claim_exclusions/source_refs",
                        [node_id, *unbound_refs],
                        "claim_exclusions 必须保留可追溯来源：KYM Target exclusions、authority/truth model、source contract、original Goal non-goals 或明确 review finding；无法绑定的 source_refs 不能作为边界依据。",
                        {
                            "unbound_source_refs": unbound_refs,
                            "bound_node_refs": sorted(
                                set(node.get("source_statement_refs", []))
                                | set(node.get("traceability_refs", []))
                                | _mapped_requirement_refs_for_node(model, node_id)
                            ),
                        },
                        current_node_ids=[node_id],
                        suggested_node_ids=["source_statement_refs", "traceability_refs", "review-finding-pdi-6"],
                    )
                )
        if not risk_reasons:
            continue
        if not has_claim_exclusions and not has_not_applicable:
            findings.append(
                _finding(
                    f"{code_prefix}_M1_CLAIM_EXCLUSION_BOUNDARY_MISSING",
                    "error",
                    "tco_model",
                    f"/nodes/{node_id}/claim_exclusions",
                    [node_id, *risk_reasons],
                    "高语义风险 M1 必须声明 source-backed claim_exclusions/forbidden claims，或提供 claim_exclusions_not_applicable rationale；不得用空 excluded_outputs 冒充 claim boundary。",
                    {
                        "risk_reasons": risk_reasons,
                        "excluded_outputs": node.get("excluded_outputs", []),
                        "authority_owner": node.get("decision_semantics", {}).get("authority_owner", ""),
                    },
                    current_node_ids=[node_id],
                    suggested_node_ids=["claim_exclusions", "claim_exclusions_not_applicable"],
                )
            )
    return findings


def _audit_tco_candidate_readiness_responsibility(
    model: dict[str, Any],
    *,
    code_prefix: str,
) -> list[Finding]:
    findings: list[Finding] = []
    for node in model.get("nodes", []):
        if node.get("node_kind") not in {"m1", "capability"}:
            continue
        semantic = node.get("decision_semantics", {})
        node_id = str(node.get("id", ""))
        role_text = _node_role_text(node)
        is_readiness_role = CANDIDATE_READINESS_EVALUATION_RE.search(role_text) is not None
        maturity_outputs = _maturity_posture_authority_outputs(node)
        if (
            GENERATION_SYNTHESIS_CAPABILITY_RE.search(role_text)
            and maturity_outputs
            and not _has_combined_responsibility_rationale(node)
        ):
            findings.append(
                _finding(
                    f"{code_prefix}_GENERATION_MATURITY_RESPONSIBILITY_COLLAPSE",
                    "error",
                    "tco_model",
                    f"/nodes/{node_id}/decision_semantics",
                    [node_id, *maturity_outputs],
                    "generation/synthesis M1 不应同时决定 maturity、review routing、diagnostic/no-candidate posture、P06 eligibility 或 authority 输出；请拆出 candidate review readiness evaluation / honest outcome M1，或提供 explicit combined-responsibility rationale。",
                    {
                        "capability_type": node.get("capability_type", ""),
                        "maturity_posture_authority_outputs": maturity_outputs,
                    },
                    current_node_ids=[node_id],
                    suggested_node_ids=[
                        "m1-materialize-grounded-candidate",
                        "m1-candidate-review-readiness-evaluation",
                        "m1-record-honest-outcome",
                    ],
                )
            )

        if not is_readiness_role:
            continue
        generation_outputs = _candidate_generation_outputs(node)
        if generation_outputs:
            findings.append(
                _finding(
                    f"{code_prefix}_READINESS_SYNTHESIS_COMPOSITE",
                    "error",
                    "tco_model",
                    f"/nodes/{node_id}/outputs",
                    [node_id, *generation_outputs],
                    "candidate review readiness M1 不得同时生成 candidate hypothesis；请将 materialization 与 P05 readiness evaluation 拆分。",
                    {"candidate_generation_outputs": generation_outputs},
                    current_node_ids=[node_id],
                    suggested_node_ids=[
                        "m1-materialize-grounded-candidate",
                        "m1-candidate-review-readiness-evaluation",
                    ],
                )
            )
        acceptance_predicates = _mapped_acceptance_predicates(model, node_id)
        readiness_boundary_text = _joined_text(
            [
                node.get("invariants", []),
                node.get("negative_fixture_refs", []),
                semantic.get("decision_rules", []),
            ]
        )
        missing = []
        if not node.get("inputs"):
            missing.append("inputs")
        if not node.get("outputs"):
            missing.append("outputs")
        authority_owner = str(semantic.get("authority_owner", "")).strip()
        if not authority_owner or authority_owner == "unresolved":
            missing.append("owner")
        if not node.get("witness_artifact_refs"):
            missing.append("witness")
        if not node.get("negative_fixture_refs"):
            missing.append("negative_fixtures")
        if not acceptance_predicates:
            missing.append("acceptance_predicates")
        if not READINESS_BOUNDARY_INVARIANT_RE.search(readiness_boundary_text):
            missing.append("review_ready_p06_eligible_formal_m1_boundary")
        if missing:
            findings.append(
                _finding(
                    f"{code_prefix}_READINESS_M1_SPLIT_CONTRACT_INCOMPLETE",
                    "error",
                    "tco_model",
                    f"/nodes/{node_id}",
                    [node_id, *missing],
                    "candidate review readiness M1 只有在具备独立 inputs、outputs、owner、witness、negative fixtures、acceptance predicates，并显式保持 review_ready != P06 eligible != Formal M1 时才可拆分；否则不能只为树形美化拆节点。",
                    {
                        "missing_contract_parts": missing,
                        "mapped_acceptance_predicates": acceptance_predicates,
                    },
                    current_node_ids=[node_id],
                )
            )
    return findings


def _audit_tco_requirement_semantic_compatibility(
    model: dict[str, Any],
    *,
    code_prefix: str,
) -> list[Finding]:
    findings: list[Finding] = []
    nodes_by_id = {node["id"]: node for node in model["nodes"]}

    for profile_index, profile in enumerate(model["target_profiles"]):
        for req_index, requirement in enumerate(profile["requirements"]):
            if not _requires_failure_taxonomy(requirement):
                continue
            path = f"/target_profiles/{profile_index}/requirements/{req_index}"
            req_id = str(requirement["id"])
            mapped_nodes = [nodes_by_id[node_id] for node_id in requirement["node_ids"] if node_id in nodes_by_id]
            mapped_capabilities = [node for node in mapped_nodes if node.get("node_kind") in {"m1", "capability"}]
            compatible_nodes = [node for node in mapped_capabilities if _supports_failure_classifier(node)]
            mapped_summary = [
                {
                    "id": node["id"],
                    "title": node.get("title", ""),
                    "capability_type": node.get("capability_type", ""),
                    "purpose": node.get("purpose", ""),
                    "source_statement_refs": node.get("source_statement_refs", []),
                    "decision_outputs": node.get("decision_semantics", {}).get("decision_outputs", []),
                    "witness_artifact_refs": node.get("witness_artifact_refs", []),
                    "negative_fixture_refs": node.get("negative_fixture_refs", []),
                    "matched_failure_taxonomy_classes": _failure_taxonomy_classes(_semantic_text_for_node(node)),
                }
                for node in mapped_capabilities
            ]
            if mapped_capabilities and not compatible_nodes:
                findings.append(
                    _finding(
                        f"{code_prefix}_REQUIREMENT_SEMANTIC_COMPATIBILITY_MISMATCH",
                        "error",
                        "tco_model",
                        f"{path}/node_ids",
                        [profile["id"], req_id, *requirement["node_ids"]],
                        "Profile requirement 要求 failure taxonomy / failure classification，但映射 M1 未体现独立失败根因分类能力；不能只因 node_ids 存在就视为承载，应拆出 m1-provider-failure-classification 或形成 unresolved gap。",
                        {
                            "source_requirement_ref": requirement.get("source_requirement_ref", ""),
                            "requirement_title": requirement.get("title", ""),
                            "required_baseline_classes": sorted(FAILURE_TAXONOMY_BASELINE_PATTERNS),
                            "mapped_m1_summary": mapped_summary,
                        },
                        current_node_ids=requirement["node_ids"],
                        suggested_node_ids=["m1-provider-failure-classification"],
                    )
                )

            bad_refs = _failure_classifier_bad_evidence_refs(requirement, mapped_capabilities)
            if bad_refs:
                findings.append(
                    _finding(
                        f"{code_prefix}_REQUIREMENT_FAILURE_CLASSIFIER_EVIDENCE_MISMATCH",
                        "error",
                        "tco_model",
                        f"{path}/witness_evidence_mapping",
                        [profile["id"], req_id, *requirement["node_ids"], *bad_refs],
                        "Failure taxonomy requirement 需要结构化 failure-classification report 与专属负例；candidate artifact 或 multi-intent candidate fixture 不能作为充分 witness/NF。",
                        {"incompatible_refs": bad_refs},
                        current_node_ids=requirement["node_ids"],
                        suggested_node_ids=[
                            "witness-failure-classification-report",
                            "nf-wrong-failure-class",
                            "nf-missing-failure-class",
                            "nf-cross-truth-domain-attribution",
                            "nf-unsupported-failure-classification",
                        ],
                    )
                )

            for node in compatible_nodes:
                routing_tokens = _failure_classifier_routing_tokens(node)
                if not routing_tokens:
                    continue
                findings.append(
                    _finding(
                        f"{code_prefix}_FAILURE_CLASSIFIER_ROUTING_COMPOSITE",
                        "error",
                        "tco_model",
                        f"/nodes/{node['id']}/decision_semantics",
                        [profile["id"], req_id, node["id"], *routing_tokens],
                        "Provider failure classifier 不得同时决定 next_action、approved live rerun 或 routing；行动建议应作为 advisory reference 或另拆 routing M1。",
                        {"routing_tokens": routing_tokens},
                        current_node_ids=[node["id"]],
                        suggested_node_ids=["m1-provider-failure-classification", "m1-provider-failure-routing"],
                    )
                )
    return findings


def _semantic_mapping_refs(value: Any, prefixes: tuple[str, ...]) -> list[str]:
    refs: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                # coverage requirement IDs bind the denominator, not the semantic
                # witness/NF artifact identity we compare against the M1.
                if str(key) in {"coverage_requirement_id", "coverage_requirement_ids", "requirement_id", "requirement_ids"}:
                    continue
                visit(child)
            return
        if isinstance(item, list):
            for child in item:
                visit(child)
            return
        if not isinstance(item, str):
            return
        token = item.strip()
        canonical = _canonical_token(token)
        if "/" in token or "." in token or " " in token:
            return
        if not SEMANTIC_EVIDENCE_REF_RE.fullmatch(token):
            return
        if canonical.startswith(prefixes):
            refs.add(token)

    visit(value)
    return sorted(refs)


def _audit_tco_requirement_evidence_mapping(
    model: dict[str, Any],
    *,
    code_prefix: str,
) -> list[Finding]:
    findings: list[Finding] = []
    nodes_by_id = {node["id"]: node for node in model["nodes"]}
    bundle_index: dict[tuple[str, str, str, str], list[tuple[int, int, dict[str, Any]]]] = {}

    for profile_index, profile in enumerate(model["target_profiles"]):
        for req_index, requirement in enumerate(profile["requirements"]):
            path = f"/target_profiles/{profile_index}/requirements/{req_index}"
            req_id = str(requirement["id"])
            mapped_nodes = [nodes_by_id[node_id] for node_id in requirement["node_ids"] if node_id in nodes_by_id]
            mapped_capabilities = [node for node in mapped_nodes if node.get("node_kind") in {"m1", "capability"}]
            rationale = _has_mapping_rationale(requirement)

            source_ref = str(requirement.get("source_requirement_ref", ""))
            mapped_source_refs = {
                ref
                for node in mapped_capabilities
                for ref in node.get("source_statement_refs", [])
            }
            if source_ref and mapped_capabilities and source_ref not in mapped_source_refs and not rationale:
                findings.append(
                    _finding(
                        f"{code_prefix}_REQUIREMENT_SOURCE_REF_NOT_IN_MAPPED_M1",
                        "error",
                        "tco_model",
                        f"{path}/source_requirement_ref",
                        [profile["id"], req_id, source_ref, *requirement["node_ids"]],
                        "Profile requirement 的 source_requirement_ref 未出现在映射 M1 的 source_statement_refs；必须改正映射或提供可验证 rationale。",
                        {"source_requirement_ref": source_ref, "mapped_source_statement_refs": sorted(mapped_source_refs)},
                    )
                )

            witness_refs = _semantic_mapping_refs(requirement.get("witness_evidence_mapping", []), ("witness",))
            expected_witness = {
                ref
                for node in mapped_capabilities
                for ref in node.get("witness_artifact_refs", [])
            }
            witness_outside = sorted(set(witness_refs) - expected_witness)
            if witness_outside and expected_witness and not rationale:
                findings.append(
                    _finding(
                        f"{code_prefix}_REQUIREMENT_WITNESS_REF_OUTSIDE_M1",
                        "error",
                        "tco_model",
                        f"{path}/witness_evidence_mapping",
                        [profile["id"], req_id, *requirement["node_ids"], *witness_outside],
                        "Profile requirement 的 witness mapping 不属于映射 M1 的 witness_artifact_refs 子集。",
                        {"mapped_witness_refs": witness_refs, "m1_witness_artifact_refs": sorted(expected_witness)},
                    )
                )

            nf_refs = _semantic_mapping_refs(requirement.get("nf_evidence_mapping", []), ("nf", "negative"))
            expected_nf = {
                ref
                for node in mapped_capabilities
                for ref in node.get("negative_fixture_refs", [])
            }
            nf_outside = sorted(set(nf_refs) - expected_nf)
            if nf_outside and expected_nf and not rationale:
                findings.append(
                    _finding(
                        f"{code_prefix}_REQUIREMENT_NF_REF_OUTSIDE_M1",
                        "error",
                        "tco_model",
                        f"{path}/nf_evidence_mapping",
                        [profile["id"], req_id, *requirement["node_ids"], *nf_outside],
                        "Profile requirement 的 NF/negative mapping 不属于映射 M1 的 negative_fixture_refs 子集。",
                        {"mapped_nf_refs": nf_refs, "m1_negative_fixture_refs": sorted(expected_nf)},
                    )
                )

            validation_owner = str(requirement.get("validation_owner", "")).strip()
            authority_owners = {
                str(node.get("decision_semantics", {}).get("authority_owner", "")).strip()
                for node in mapped_capabilities
            }
            authority_owners.discard("")
            authority_owners.discard("unresolved")
            if validation_owner and validation_owner != "unresolved" and validation_owner in authority_owners and not rationale:
                findings.append(
                    _finding(
                        f"{code_prefix}_REQUIREMENT_VALIDATION_OWNER_COPIES_AUTHORITY",
                        "error",
                        "tco_model",
                        f"{path}/validation_owner",
                        [profile["id"], req_id, *requirement["node_ids"]],
                        "validation_owner 与映射 M1 authority_owner 相同；能力 owner 不应自证，除非有显式 rationale。",
                        {"validation_owner": validation_owner, "authority_owner": sorted(authority_owners)},
                    )
                )

            if _requires_lane_authority(requirement) and mapped_capabilities:
                compatible_authority_nodes = [
                    node for node in mapped_capabilities if _supports_lane_authority_capability(node)
                ]
                if not compatible_authority_nodes:
                    findings.append(
                        _finding(
                            f"{code_prefix}_REQUIREMENT_LANE_AUTHORITY_CAPABILITY_MISMATCH",
                            "error",
                            "tco_model",
                            f"{path}/node_ids",
                            [profile["id"], req_id, *requirement["node_ids"]],
                            "lane relationship / authority-boundary requirement 必须映射到 truth-boundary 或 authority-separation capability；candidate content、candidate shape 或普通 readiness M1 不能只凭 node_ids 承载该 requirement。",
                            {
                                "source_requirement_ref": source_ref,
                                "requirement_title": requirement.get("title", ""),
                                "mapped_m1_summary": _mapped_capability_summary(mapped_capabilities),
                            },
                            current_node_ids=requirement["node_ids"],
                            suggested_node_ids=["m1-preserve-truth-boundaries"],
                        )
                    )

                candidate_domain_refs = _lane_authority_candidate_domain_refs(requirement, mapped_capabilities)
                owner_domain_mismatch = bool(validation_owner and CANDIDATE_OWNER_DOMAIN_RE.search(validation_owner))
                authority_owner_domain_mismatch = sorted(
                    owner
                    for owner in authority_owners
                    if CANDIDATE_OWNER_DOMAIN_RE.search(owner)
                )
                claim_domain_mismatch = bool(
                    CANDIDATE_CLAIM_CEILING_RE.search(str(requirement.get("claim_ceiling", "")))
                )
                domain_evidence_gaps = _lane_authority_domain_evidence_gaps(requirement, mapped_capabilities)
                if (
                    candidate_domain_refs
                    or owner_domain_mismatch
                    or authority_owner_domain_mismatch
                    or claim_domain_mismatch
                    or domain_evidence_gaps
                ):
                    findings.append(
                        _finding(
                            f"{code_prefix}_REQUIREMENT_LANE_AUTHORITY_EVIDENCE_DOMAIN_MISMATCH",
                            "error",
                            "tco_model",
                            path,
                            [
                                profile["id"],
                                req_id,
                                *requirement["node_ids"],
                                *candidate_domain_refs,
                                *authority_owner_domain_mismatch,
                                *domain_evidence_gaps,
                            ],
                            "lane relationship / authority-boundary requirement 需要 truth-domain、claim-ceiling 与 authority-separation witness/NF；candidate artifact、multi-intent candidate、P05 candidate owner、candidate authority owner 或 candidate-only claim ceiling 不能作为主要 evidence bundle。",
                            {
                                "source_requirement_ref": source_ref,
                                "validation_owner": validation_owner,
                                "authority_owner_domain_mismatch": authority_owner_domain_mismatch,
                                "claim_ceiling": requirement.get("claim_ceiling", ""),
                                "candidate_domain_refs": candidate_domain_refs,
                                "owner_domain_mismatch": owner_domain_mismatch,
                                "claim_domain_mismatch": claim_domain_mismatch,
                                "domain_evidence_gaps": domain_evidence_gaps,
                                "mapped_m1_summary": _mapped_capability_summary(mapped_capabilities),
                            },
                            current_node_ids=requirement["node_ids"],
                            suggested_node_ids=[
                                "m1-preserve-truth-boundaries",
                                "witness-truth-domain-map",
                                "witness-claim-ceiling",
                                "nf-provider-as-canonical",
                                "nf-runtime-success-as-semantic",
                            ],
                        )
                    )

            bundle_key = (
                _normalize_text(validation_owner),
                json.dumps(_normal_signature(requirement.get("witness_evidence_mapping", [])), ensure_ascii=False, sort_keys=True),
                json.dumps(_normal_signature(requirement.get("nf_evidence_mapping", [])), ensure_ascii=False, sort_keys=True),
                _normalize_text(str(requirement.get("claim_ceiling", ""))),
            )
            bundle_index.setdefault(bundle_key, []).append((profile_index, req_index, requirement))

    for bundle_requirements in bundle_index.values():
        if len(bundle_requirements) < 2:
            continue
        source_refs = {str(requirement.get("source_requirement_ref", "")) for _, _, requirement in bundle_requirements}
        titles = {_normalize_text(str(requirement.get("title", ""))) for _, _, requirement in bundle_requirements}
        if len(source_refs) < 2 and len(titles) < 2:
            continue
        if any(_has_mapping_rationale(requirement) for _, _, requirement in bundle_requirements):
            continue
        subject_ids = [
            str(requirement.get("id", ""))
            for _, _, requirement in bundle_requirements
        ]
        findings.append(
            _finding(
                f"{code_prefix}_REQUIREMENT_EVIDENCE_BUNDLE_REUSED",
                "error",
                "tco_model",
                "/target_profiles/*/requirements",
                subject_ids,
                "多个语义不同 Profile requirements 复用完全相同 validation_owner、witness mapping、NF mapping 与 claim ceiling；必须拆分为能力专属映射，或提供 shared_evidence_rationale、mapping_rationale、evidence_mapping_rationale 等显式字段。",
                {
                    "requirement_ids": subject_ids,
                    "source_requirement_refs": sorted(source_refs),
                    "titles": sorted(titles),
                },
            )
        )
    return findings


def _audit_tco_structure(
    model: dict[str, Any], base_model: dict[str, Any] | None
) -> list[Finding]:
    findings: list[Finding] = []
    nodes = model["nodes"]
    node_by_id = {item["id"]: item for item in nodes}
    profiles = model["target_profiles"]
    if not profiles:
        findings.append(
            _finding(
                "TCO_TARGET_PROFILE_MISSING",
                "error",
                "tco_model",
                "/target_profiles",
                [],
                "TCO model 至少需要一个 Target Profile。",
                {"target_profile_count": 0},
            )
        )
    duplicate_profile_ids = _duplicate_values([item["id"] for item in profiles])
    if duplicate_profile_ids:
        findings.append(
            _finding(
                "TCO_PROFILE_ID_DUPLICATE",
                "error",
                "tco_model",
                "/target_profiles",
                duplicate_profile_ids,
                "Target Profile id 不唯一。",
                {"duplicate_profile_ids": duplicate_profile_ids},
            )
        )
    if base_model is not None:
        base_profiles = {item["id"] for item in base_model["target_profiles"]}
        candidate_profiles = {item["id"] for item in profiles}
        removed = sorted(base_profiles - candidate_profiles)
        if removed:
            findings.append(
                _finding(
                    "TCO_TARGET_PROFILE_REMOVED",
                    "error",
                    "tco_model",
                    "/target_profiles",
                    removed,
                    "候选 TCO model 删除了 base 中的 Target Profile。",
                    {"removed_target_profile_ids": removed},
                )
            )
        base_source_targets = {
            item["source_target_id"] for item in base_model["target_profiles"]
        }
        candidate_source_targets = {item["source_target_id"] for item in profiles}
        removed_source_targets = sorted(base_source_targets - candidate_source_targets)
        if removed_source_targets:
            findings.append(
                _finding(
                    "TCO_SOURCE_TARGET_REMOVED",
                    "error",
                    "tco_model",
                    "/target_profiles",
                    removed_source_targets,
                    "候选 TCO model 不再包含 base 中已建模的 source Target。",
                    {"removed_source_target_ids": removed_source_targets},
                )
            )

        candidate_node_ids = set(node_by_id)
        referenced_base_nodes = {
            node_id
            for profile in base_model["target_profiles"]
            for requirement in profile["requirements"]
            for node_id in requirement["node_ids"]
        }
        removed_referenced_nodes = sorted(referenced_base_nodes - candidate_node_ids)
        if removed_referenced_nodes:
            findings.append(
                _finding(
                    "TCO_REFERENCED_NODE_REMOVED",
                    "warning",
                    "tco_model",
                    "/nodes",
                    removed_referenced_nodes,
                    "候选删除了 base Target mapping 曾引用的 M，需要在模型 delta 中人工审查。",
                    {"removed_referenced_node_ids": removed_referenced_nodes},
                )
            )

        base_by_id = {item["id"]: item for item in base_model["target_profiles"]}
        candidate_by_id = {item["id"]: item for item in profiles}
        for profile_id in sorted(set(base_by_id) & set(candidate_by_id)):
            base_requirements = {
                item["source_requirement_ref"]: item
                for item in base_by_id[profile_id]["requirements"]
            }
            candidate_requirements = {
                item["source_requirement_ref"]: item
                for item in candidate_by_id[profile_id]["requirements"]
            }
            for source_ref in sorted(set(base_requirements) & set(candidate_requirements)):
                before = sorted(set(base_requirements[source_ref]["node_ids"]))
                after = sorted(set(candidate_requirements[source_ref]["node_ids"]))
                if before == after:
                    continue
                findings.append(
                    _finding(
                        "TCO_REQUIREMENT_MAPPING_CHANGED",
                        "warning",
                        "tco_model",
                        f"/target_profiles/{profile_id}/requirements/{source_ref}/node_ids",
                        [profile_id, source_ref, *before, *after],
                        "Target requirement 的 M mapping 相对 base 发生变化，需要显式审查语义 delta。",
                        {"before_node_ids": before, "after_node_ids": after},
                    )
                )

    for node in nodes:
        duplicates = _duplicate_values(node["scope"])
        if duplicates:
            findings.append(
                _finding(
                    "TCO_SCOPE_DUPLICATE",
                    "error",
                    "tco_model",
                    f"/nodes/{node['id']}/scope",
                    [node["id"]],
                    "TCO node 的 Scope 包含重复项。",
                    {"duplicate_scope_items": duplicates, "node_id": node["id"]},
                )
            )
    for profile in profiles:
        for requirement in profile["requirements"]:
            duplicates = _duplicate_values(requirement["node_ids"])
            if duplicates:
                findings.append(
                    _finding(
                        "TCO_REQUIREMENT_NODE_DUPLICATE",
                        "error",
                        "tco_model",
                        f"/target_profiles/{profile['id']}/requirements/{requirement['id']}/node_ids",
                        [profile["id"], requirement["id"], *duplicates],
                        "Target requirement 的 node_ids 包含重复项。",
                        {"duplicate_node_ids": duplicates},
                    )
                )
            group_ids = sorted(
                node_id
                for node_id in set(requirement["node_ids"])
                if node_id in node_by_id and node_by_id[node_id]["node_kind"] == "group"
            )
            if group_ids:
                findings.append(
                    _finding(
                        "TCO_REQUIREMENT_COUNTS_GROUP",
                        "error",
                        "tco_model",
                        f"/target_profiles/{profile['id']}/requirements/{requirement['id']}/node_ids",
                        [profile["id"], requirement["id"], *group_ids],
                        "Target requirement 把仅用于组织结构的 group node 计入能力映射。",
                        {"group_node_ids": group_ids},
                    )
                )

    tsp_groups: dict[tuple[str, tuple[str, ...], str], list[str]] = {}
    for node in nodes:
        key = (
            _normalize_text(node["topic"]),
            tuple(_normalize_text(item) for item in node["scope"]),
            _normalize_text(node["purpose"]),
        )
        tsp_groups.setdefault(key, []).append(node["id"])
    for node_ids in sorted(tsp_groups.values(), key=lambda item: tuple(sorted(item))):
        if len(node_ids) < 2:
            continue
        ordered_ids = sorted(node_ids)
        findings.append(
            _finding(
                "TCO_TSP_EXACT_DUPLICATE",
                "warning",
                "tco_model",
                "/nodes",
                ordered_ids,
                "多个 TCO nodes 使用完全相同的 Topic/Scope/Purpose，请检查是否过度拆分。",
                {"node_ids": ordered_ids},
            )
        )
    return findings


def _coverage_tokens(config: dict[str, Any]) -> list[str]:
    return sorted(
        {item["id"].casefold() for item in config["coverage_vocabulary"]["states"]},
        key=lambda item: (-len(item), item),
    )


def _audit_tsp_vocabulary(model: dict[str, Any], config: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    tokens = _coverage_tokens(config)
    for node in model["nodes"]:
        scope = node["scope"]
        if isinstance(scope, dict):
            boundary = scope.get("boundary", {})
            scope_values = [
                *boundary.get("included", []),
                *boundary.get("excluded", []),
            ]
        else:
            scope_values = scope
        for field, values in (
            ("topic", [node["topic"]]),
            ("scope", scope_values),
            ("purpose", [node["purpose"]]),
        ):
            matched: set[str] = set()
            for value in values:
                folded = value.casefold()
                if "coverage_state" in folded or "coverage state" in folded or "覆盖状态" in value:
                    matched.add("coverage_state")
                for token in tokens:
                    if re.search(rf"(?<![a-z0-9_]){re.escape(token)}(?![a-z0-9_])", folded):
                        matched.add(token)
            if not matched:
                continue
            findings.append(
                _finding(
                    "TCO_TSP_COVERAGE_VOCABULARY_LEAK",
                    "warning",
                    "tco_model",
                    f"/nodes/{node['id']}/{field}",
                    [node["id"]],
                    "TSP 字段混入 Coverage 状态词，请把能力边界与证据状态分开。",
                    {"field": field, "matched_tokens": sorted(matched)},
                )
            )
    return findings


def _audit_coverage(
    coverage: dict[str, Any], config: dict[str, Any]
) -> list[Finding]:
    findings: list[Finding] = []
    if coverage["coverage_mode"] == "not_evaluated":
        return findings
    not_started_rank = state_rank(config, "not_started")
    for record in coverage["records"]:
        node_id = record["tco_node_id"]
        direct_rank = state_rank(config, record["coverage_state"])
        scope_ranks = [
            (item["scope"], item["coverage_state"], state_rank(config, item["coverage_state"]))
            for item in record["scope_coverage"]
        ]
        if scope_ranks:
            weakest_scope, weakest_state, weakest_rank = min(
                scope_ranks, key=lambda item: (item[2], item[0], item[1])
            )
            if direct_rank > weakest_rank:
                findings.append(
                    _finding(
                        "TCO_DIRECT_STATE_EXCEEDS_SCOPE",
                        "warning",
                        "coverage",
                        f"/records/{node_id}",
                        [node_id],
                        "direct Coverage state 高于最弱 Scope；effective state 将按最弱项保守汇总。",
                        {
                            "direct_state": record["coverage_state"],
                            "weakest_scope": weakest_scope,
                            "weakest_scope_state": weakest_state,
                        },
                    )
                )
        declared_ranks = [direct_rank, *(item[2] for item in scope_ranks)]
        advanced = max(declared_ranks) > not_started_rank
        if advanced and not record["claim_ceiling"].strip():
            findings.append(
                _finding(
                    "TCO_ADVANCED_STATE_MISSING_CLAIM_CEILING",
                    "warning",
                    "coverage",
                    f"/records/{node_id}/claim_ceiling",
                    [node_id],
                    "Coverage 已声明高于 not_started 的状态，但 claim_ceiling 为空。",
                    {"declared_states": [record["coverage_state"], *[item[1] for item in scope_ranks]]},
                )
            )
        if advanced and record["missing_scope_items"]:
            findings.append(
                _finding(
                    "TCO_MISSING_SCOPE_ITEMS_WITH_ADVANCED_STATE",
                    "warning",
                    "coverage",
                    f"/records/{node_id}/missing_scope_items",
                    [node_id],
                    "Coverage 的较高状态与未覆盖 Scope 项同时存在，需要人工核对声明上限。",
                    {"missing_scope_items": sorted(record["missing_scope_items"])},
                )
            )
    return findings


def _configuration_identity(row: dict[str, Any]) -> str | None:
    for key in CONFIGURATION_IDENTITY_KEYS:
        value = row.get(key)
        if value:
            return f"{key}={value}"
    dimensions = [
        f"{key}={row[key]}"
        for key in CONFIGURATION_DIMENSION_KEYS
        if row.get(key)
    ]
    return "|".join(dimensions) if dimensions else None


def _comparability_verdict(row: dict[str, Any]) -> str | None:
    for key in COMPARABILITY_VERDICT_KEYS:
        value = row.get(key)
        if value:
            return _canonical_token(str(value))
    return None


def _comparison_eligibility(row: dict[str, Any]) -> str | None:
    for key in COMPARABILITY_ELIGIBILITY_KEYS:
        value = row.get(key)
        if value is not None:
            return _canonical_token(str(value))
    if row.get("comparison_eligible") is not None:
        return _canonical_token(str(row["comparison_eligible"]))
    return None


def _claims_same_model_from_alias(row: dict[str, Any]) -> bool:
    if row.get("same_model_claim") is True or row.get("same_configuration_claim") is True:
        return True
    text = _joined_text(
        [
            row.get("claim", ""),
            row.get("notes", ""),
            row.get("comparability_verdict", ""),
            row.get("comparison_verdict", ""),
            row.get("configuration_comparability", ""),
        ]
    )
    return SAME_MODEL_CLAIM_RE.search(text) is not None


def _has_immutable_model_resolution(row: dict[str, Any]) -> bool:
    return any(row.get(key) for key in IMMUTABLE_MODEL_RESOLUTION_KEYS)


def _audit_v4_breadth_configuration_comparability(
    coverage: dict[str, Any],
    config: dict[str, Any],
    *,
    code_prefix: str,
) -> list[Finding]:
    findings: list[Finding] = []
    if coverage.get("coverage_mode") != "repo":
        return findings
    for record_index, record in enumerate(coverage.get("records", [])):
        node_id = str(record.get("tco_node_id", ""))
        active_breadth_levels = sorted(
            level
            for level, state in record.get("breadth_coverage", {}).items()
            if level in {"multi_repo", "cross_language", "arbitrary_attempt"}
            and state_rank(config, state) > state_rank(config, "not_started")
        )
        breadth_rows = [
            row
            for row in record.get("breadth_evidence", [])
            if isinstance(row, dict)
        ]
        for row_index, row in enumerate(breadth_rows):
            alias = row.get("model_alias") or row.get("observed_model_alias") or row.get("provider_model_alias")
            if alias and _claims_same_model_from_alias(row) and not _has_immutable_model_resolution(row):
                findings.append(
                    _finding(
                        f"{code_prefix}_BREADTH_MODEL_ALIAS_WITHOUT_IMMUTABLE_RESOLUTION",
                        "error",
                        "coverage",
                        f"/records/{record_index}/breadth_evidence/{row_index}",
                        [node_id, str(row.get("id", "")), str(alias)],
                        "Model alias 没有 immutable resolution evidence 时只能记录 observed alias/configuration；不得声称同模型复现或 same_configuration。",
                        {
                            "model_alias": alias,
                            "same_model_claim": row.get("same_model_claim"),
                            "available_resolution_keys": sorted(
                                key for key in IMMUTABLE_MODEL_RESOLUTION_KEYS if row.get(key)
                            ),
                        },
                        current_node_ids=[node_id],
                    )
                )
        if not active_breadth_levels:
            continue
        identities = sorted({
            identity
            for identity in (_configuration_identity(row) for row in breadth_rows)
            if identity
        })
        if len(identities) <= 1:
            continue
        rows_with_config = [row for row in breadth_rows if _configuration_identity(row)]
        missing_verdict_rows = [
            str(row.get("id", ""))
            for row in rows_with_config
            if _comparability_verdict(row) not in COMPARABILITY_VERDICTS
        ]
        if missing_verdict_rows:
            findings.append(
                _finding(
                    f"{code_prefix}_BREADTH_CONFIGURATION_COMPARABILITY_MISSING",
                    "error",
                    "coverage",
                    f"/records/{record_index}/breadth_evidence",
                    [node_id, *missing_verdict_rows],
                    "multi-repo/multi-trial breadth evidence 混用了不同 provider configuration；必须有 comparability verdict，不能把不同配置直接聚合为 breadth evidence。",
                    {
                        "active_breadth_levels": active_breadth_levels,
                        "configuration_identities": identities,
                        "rows_missing_verdict": missing_verdict_rows,
                    },
                    current_node_ids=[node_id],
                    suggested_node_ids=["m1-provider-configuration-version-management"],
                )
            )
            continue
        non_comparable_rows = [
            str(row.get("id", ""))
            for row in rows_with_config
            if _comparability_verdict(row) in NON_COMPARABLE_VERDICTS
        ]
        if non_comparable_rows:
            findings.append(
                _finding(
                    f"{code_prefix}_BREADTH_CONFIGURATION_NON_COMPARABLE_AGGREGATED",
                    "error",
                    "coverage",
                    f"/records/{record_index}/breadth_evidence",
                    [node_id, *non_comparable_rows],
                    "`non_comparable` 或 `comparability_unknown` 配置关系不能支撑高级 breadth 聚合；应降级 Coverage breadth 或拆出 configuration lineage/gap。",
                    {
                        "active_breadth_levels": active_breadth_levels,
                        "configuration_identities": identities,
                        "non_comparable_rows": non_comparable_rows,
                    },
                    current_node_ids=[node_id],
                    suggested_node_ids=["m1-provider-configuration-version-management"],
                )
            )
            continue
        contradictory_rows = [
            str(row.get("id", ""))
            for row in rows_with_config
            if _comparability_verdict(row) == "same_configuration"
        ]
        if contradictory_rows:
            findings.append(
                _finding(
                    f"{code_prefix}_BREADTH_CONFIGURATION_SAME_VERDICT_CONTRADICTS_IDENTITY",
                    "error",
                    "coverage",
                    f"/records/{record_index}/breadth_evidence",
                    [node_id, *contradictory_rows],
                    "`same_configuration` verdict 不能与多个不同 configuration identity 同时支撑 breadth 聚合；需要 immutable identity reconciliation 或改为 variant/non-comparable posture。",
                    {
                        "active_breadth_levels": active_breadth_levels,
                        "configuration_identities": identities,
                        "same_configuration_rows": contradictory_rows,
                    },
                    current_node_ids=[node_id],
                    suggested_node_ids=["m1-provider-configuration-version-management"],
                )
            )
            continue
        ineligible_rows = [
            str(row.get("id", ""))
            for row in rows_with_config
            if _comparison_eligibility(row) not in COMPARISON_ELIGIBLE_VALUES
        ]
        if ineligible_rows:
            findings.append(
                _finding(
                    f"{code_prefix}_BREADTH_CONFIGURATION_COMPARISON_ELIGIBILITY_MISSING",
                    "error",
                    "coverage",
                    f"/records/{record_index}/breadth_evidence",
                    [node_id, *ineligible_rows],
                    "不同 configuration variant 只有在声明 comparison eligibility 后才能支撑高级 breadth 聚合；否则只能记录为 variant trial 或 comparability_unknown。",
                    {
                        "active_breadth_levels": active_breadth_levels,
                        "configuration_identities": identities,
                        "rows_missing_eligibility": ineligible_rows,
                    },
                    current_node_ids=[node_id],
                    suggested_node_ids=["m1-provider-configuration-version-management"],
                )
                )
    return findings


def _audit_v4_orthogonal_state_axes(
    coverage: dict[str, Any],
    config: dict[str, Any],
    *,
    code_prefix: str,
) -> list[Finding]:
    findings: list[Finding] = []
    if coverage.get("coverage_mode") != "repo":
        return findings
    for record_index, record in enumerate(coverage.get("records", [])):
        node_id = str(record.get("tco_node_id", ""))
        lifecycle = str(record.get("capability_lifecycle_state", ""))
        if lifecycle not in CAPABILITY_LIFECYCLE_STATES or lifecycle in LIFECYCLE_AXIS_FORBIDDEN_VALUES:
            findings.append(
                _finding(
                    f"{code_prefix}_LIFECYCLE_AXIS_VALUE_COLLAPSE",
                    "error",
                    "coverage",
                    f"/records/{record_index}/capability_lifecycle_state",
                    [node_id, lifecycle],
                    "capability_lifecycle_state 只能表达 draft/contract_defined/implemented/maintained/deprecated；不得混入 Coverage、breadth、validation posture 或 resolution status。",
                    {
                        "actual": lifecycle,
                        "allowed_lifecycle_values": sorted(CAPABILITY_LIFECYCLE_STATES),
                        "separate_axes": {
                            "validation_posture": sorted(VALIDATION_POSTURES),
                            "breadth": sorted(BREADTH_LEVELS),
                            "resolution_status": sorted(RESOLUTION_STATUSES),
                        },
                    },
                    current_node_ids=[node_id],
                )
            )
        if lifecycle == "maintained":
            missing = [
                key
                for key in ("maintained_registration_ref", "lifecycle_owner", "continuous_gate_ref")
                if not str(record.get(key, "")).strip()
            ]
            if missing:
                findings.append(
                    _finding(
                        f"{code_prefix}_MAINTAINED_LIFECYCLE_REGISTRATION_MISSING",
                        "error",
                        "coverage",
                        f"/records/{record_index}",
                        [node_id],
                        "maintained lifecycle 必须有 maintained registration、owner 与持续 gate；不能由 test pass、independent validation 或 bounded_runtime 自动获得。",
                        {"missing": missing},
                        current_node_ids=[node_id],
                    )
                )
        if lifecycle == "deprecated" and not (
            str(record.get("replacement_ref", "")).strip()
            or str(record.get("sunset_ref", "")).strip()
        ):
            findings.append(
                _finding(
                    f"{code_prefix}_DEPRECATED_LIFECYCLE_REFERENCE_MISSING",
                    "error",
                    "coverage",
                    f"/records/{record_index}",
                    [node_id],
                    "deprecated 是 lifecycle 终止分支，必须绑定 replacement_ref 或 sunset_ref，不能作为普通递增 rank 满足 maintained Target。",
                    {},
                    current_node_ids=[node_id],
                )
            )
        validation_posture = str(record.get("validation_posture", ""))
        if validation_posture not in VALIDATION_POSTURES:
            findings.append(
                _finding(
                    f"{code_prefix}_VALIDATION_POSTURE_INVALID",
                    "error",
                    "coverage",
                    f"/records/{record_index}/validation_posture",
                    [node_id, validation_posture],
                    "validation_posture 必须独立表达 not_validated/producer_checked/independently_validated，不能写入 lifecycle 或 Coverage state。",
                    {"actual": validation_posture},
                    current_node_ids=[node_id],
                )
            )
        resolution_status = str(record.get("resolution_status", ""))
        if resolution_status not in RESOLUTION_STATUSES:
            findings.append(
                _finding(
                    f"{code_prefix}_RESOLUTION_STATUS_INVALID",
                    "error",
                    "coverage",
                    f"/records/{record_index}/resolution_status",
                    [node_id, resolution_status],
                    "resolution_status 只表达语义/建模问题是否 resolved、unresolved 或 disputed；不得继续塞进 lifecycle。",
                    {"actual": resolution_status},
                    current_node_ids=[node_id],
                )
            )
        coverage_values = [
            ("coverage_state", record.get("coverage_state")),
            ("evidence_peak_state", record.get("evidence_peak_state")),
            ("effective_state", record.get("effective_state")),
            ("claim_state", record.get("claim_state")),
            *[
                (f"requirement_coverage/{index}", row.get("coverage_state"))
                for index, row in enumerate(record.get("requirement_coverage", []))
            ],
        ]
        bad_coverage_values = [
            {"field": field, "value": value}
            for field, value in coverage_values
            if str(value) in COVERAGE_AXIS_FORBIDDEN_VALUES
        ]
        if bad_coverage_values:
            findings.append(
                _finding(
                    f"{code_prefix}_COVERAGE_AXIS_VALUE_COLLAPSE",
                    "error",
                    "coverage",
                    f"/records/{record_index}",
                    [node_id, *[str(item["value"]) for item in bad_coverage_values]],
                    "Coverage/evidence state 只能使用 Coverage vocabulary；不得写入 maintained、lifecycle、validation posture、resolution status 或 breadth evidence 值。",
                    {"bad_values": bad_coverage_values},
                    current_node_ids=[node_id],
                )
            )
        breadth = record.get("breadth_coverage", {})
        for level, state in breadth.items():
            if level not in BREADTH_LEVELS:
                findings.append(
                    _finding(
                        f"{code_prefix}_BREADTH_LEVEL_INVALID",
                        "error",
                        "coverage",
                        f"/records/{record_index}/breadth_coverage/{level}",
                        [node_id, str(level)],
                        "breadth axis 只能使用 single_trial/single_repo/known_family/multi_repo/cross_language/arbitrary_attempt。",
                        {"level": level, "allowed_breadth_levels": sorted(BREADTH_LEVELS)},
                        current_node_ids=[node_id],
                    )
                )
                continue
            if level == "multi_repo" and state_rank(config, state) > state_rank(config, "not_started"):
                rows = [row for row in record.get("breadth_evidence", []) if isinstance(row, dict)]
                repo_ids = {str(row.get("repo_id", "")) for row in rows if row.get("repo_id")}
                rows_missing_identity = [
                    str(row.get("id", ""))
                    for row in rows
                    if not row.get("id") or not row.get("repo_id") or not row.get("evidence_ref")
                ]
                if len(repo_ids) < 2 or rows_missing_identity:
                    findings.append(
                        _finding(
                            f"{code_prefix}_MULTI_REPO_BREADTH_IDENTITY_MATRIX_MISSING",
                            "error",
                            "coverage",
                            f"/records/{record_index}/breadth_evidence",
                            [node_id, *sorted(repo_ids)],
                            "multi_repo breadth 必须绑定具体 repo identity matrix、row id 与 evidence_ref；不能由 bounded_runtime 或单 repo evidence 自动推出。",
                            {
                                "repo_ids": sorted(repo_ids),
                                "rows_missing_identity": rows_missing_identity,
                            },
                            current_node_ids=[node_id],
                        )
                    )
    return findings


def audit_tco_candidate(
    workspace: Path,
    product_id: str,
    model_path: Path,
    coverage_path: Path | None = None,
    base_model_path: Path | None = None,
) -> dict[str, Any]:
    root = project_root(workspace, product_id)
    config = load_config(root)
    model = load_json(model_path)
    if model.get("schema") == "kym_tco.tco_model.v4":
        validate_document("tco_model_v4", model, model_path)
        _require_product(model, product_id, "TCO v4 model")
        findings: list[Finding] = []
        if model.get("candidate_status") != "candidate_not_approved":
            findings.append(_finding("TCO_V4_MODEL_CANDIDATE_STATUS_INVALID", "error", "tco_model", "/candidate_status", [], "v4 model 必须保持 candidate_not_approved。", {}))
        nodes_by_id = {node["id"]: node for node in model["nodes"]}
        counted_node_ids = {node_id for node_id, node in nodes_by_id.items() if node["node_kind"] in {"m1", "policy"}}
        forbidden_aliases = {"reject", "rejected"} | {
            _canonical_token(value)
            for value in config["decision_outcome_vocabulary"].get("forbidden_aliases", [])
        }
        for node in model["nodes"]:
            legacy_fields = sorted(set(node) & {"scope_types", "mandatory_scope_items", "effect_status"})
            if legacy_fields:
                findings.append(_finding("TCO_V4_LEGACY_SCOPE_OR_EXECUTION_FIELD", "error", "tco_model", f"/nodes/{node['id']}", [node["id"]], "v4 node 不得携带 legacy Scope 分母或 execution effect_status 字段。", {"legacy_fields": legacy_fields}))
            if node["node_kind"] == "m1":
                required = {"capability_type", "inputs", "outputs", "decision_semantics", "metadata_requirements", "coverage_requirements", "mandatory_coverage_requirement_ids", "invariants", "witness_artifact_refs", "negative_fixture_refs", "traceability_refs"}
                missing = sorted(required - set(node))
                if missing or node["children"]:
                    findings.append(_finding("TCO_V4_M1_INCOMPLETE", "error", "tco_model", f"/nodes/{node['id']}", [node["id"]], "v4 M1 必须具有完整独立能力字段且没有 children。", {"missing": missing, "children": node["children"]}))
                semantic = node.get("decision_semantics", {})
                if len(node.get("inputs", [])) > 2 and len(node.get("outputs", [])) > 2 and len(semantic.get("decision_rules", [])) > 2:
                    findings.append(_finding("TCO_V4_GOD_M1_REVIEW", "warning", "tco_model", f"/nodes/{node['id']}", [node["id"]], "M1 同时承担多输入、多输出、多决策规则，需复核是否应拆分。", {"input_count": len(node.get("inputs", [])), "output_count": len(node.get("outputs", [])), "decision_rule_count": len(semantic.get("decision_rules", []))}))
                text = " ".join(str(node.get(key, "")) for key in ("title", "purpose", "capability_type"))
                if ARTIFACT_ONLY_RE.search(text):
                    findings.append(_finding("TCO_V4_ARTIFACT_ONLY_M1_REVIEW", "warning", "tco_model", f"/nodes/{node['id']}", [node["id"]], "M1 看起来像产物/文档本身，需确认其表达的是可独立验证的能力。", {"text": text}))
                authority_owner = str(semantic.get("authority_owner", ""))
                if authority_owner and authority_owner != "unresolved" and MULTI_AUTHORITY_RE.search(authority_owner):
                    findings.append(_finding("TCO_V4_MULTI_AUTHORITY_OWNER", "error", "tco_model", f"/nodes/{node['id']}/decision_semantics/authority_owner", [node["id"]], "M1 只能有一个决策 authority owner。", {"authority_owner": authority_owner}))
                alias_hits = sorted(
                    value
                    for value in [
                        *semantic.get("decision_outputs", []),
                        *semantic.get("terminal_outcomes", []),
                        *semantic.get("failure_outcomes", []),
                    ]
                    if _canonical_token(value) in forbidden_aliases
                )
                if alias_hits:
                    findings.append(_finding("TCO_V4_CONTEXT_FREE_OUTCOME_ALIAS", "error", "tco_model", f"/nodes/{node['id']}/decision_semantics", [node["id"]], "v4 decision/terminal/failure outcome 不得使用无上下文 reject/rejected 或 forbidden alias。", {"alias_hits": alias_hits}))
                input_only_outcomes = sorted(
                    value
                    for value in [*semantic.get("terminal_outcomes", []), *semantic.get("failure_outcomes", [])]
                    if _canonical_token(value) in V4_INPUT_ONLY_TOKENS
                )
                if input_only_outcomes:
                    findings.append(_finding("TCO_V4_INPUT_TOKEN_AS_OUTCOME", "error", "tco_model", f"/nodes/{node['id']}/decision_semantics", [node["id"]], "v4 不得把 input-only token 建模为 outcome 或 failure。", {"input_only_outcomes": input_only_outcomes}))
                metadata_artifacts = sorted({
                    value
                    for item in node.get("metadata_requirements", [])
                    for value in (str(item.get("id", "")), str(item.get("label", "")))
                    if _canonical_token(value) in V4_OUTPUT_ONLY_ARTIFACT_TOKENS
                })
                if metadata_artifacts:
                    findings.append(_finding("TCO_V4_ARTIFACT_AS_METADATA_REQUIREMENT", "error", "tco_model", f"/nodes/{node['id']}/metadata_requirements", [node["id"]], "P06Assessment/P06ValidationReport/P06DecisionRecord 必须作为 outputs，不得作为 metadata_requirements。", {"metadata_artifacts": metadata_artifacts}))
                declared_tokens = {
                    _canonical_token(value)
                    for value in [
                        *[item.get("id", "") for item in node.get("inputs", [])],
                        *[item.get("id", "") for item in node.get("outputs", [])],
                        *[item.get("id", "") for item in node.get("metadata_requirements", [])],
                        *semantic.get("decision_outputs", []),
                        *semantic.get("terminal_outcomes", []),
                    ]
                }
                excluded_overlap = sorted(
                    value
                    for value in node.get("excluded_outputs", [])
                    if _canonical_token(value) in declared_tokens
                )
                if excluded_overlap:
                    findings.append(_finding("TCO_V4_EXCLUDED_OUTPUT_OVERLAP", "error", "tco_model", f"/nodes/{node['id']}/excluded_outputs", [node["id"]], "v4 excluded_outputs 不得与 declared inputs/outputs/decision outputs/outcomes/metadata 重叠。", {"excluded_overlap": excluded_overlap}))
                coverage_ids = [item["id"] for item in node.get("coverage_requirements", [])]
                mandatory_ids = set(node.get("mandatory_coverage_requirement_ids", []))
                mandatory_flags = {item["id"] for item in node.get("coverage_requirements", []) if item.get("mandatory")}
                if mandatory_ids != mandatory_flags or mandatory_ids - set(coverage_ids):
                    findings.append(_finding("TCO_V4_MANDATORY_REQUIREMENT_DRIFT", "error", "tco_model", f"/nodes/{node['id']}/mandatory_coverage_requirement_ids", [node["id"]], "mandatory_coverage_requirement_ids 必须等于 coverage_requirements 中 mandatory=true 的稳定 ID。", {"mandatory_ids": sorted(mandatory_ids), "mandatory_flags": sorted(mandatory_flags)}))
                input_only_requirement_refs = sorted(
                    item["id"]
                    for item in node.get("coverage_requirements", [])
                    if item.get("category") in {"outcome", "failure"}
                    and _canonical_token(str(item.get("ref", "")).rsplit(".", 1)[-1]) in V4_INPUT_ONLY_TOKENS
                )
                if input_only_requirement_refs:
                    findings.append(_finding("TCO_V4_INPUT_TOKEN_AS_OUTCOME_REQUIREMENT", "error", "tco_model", f"/nodes/{node['id']}/coverage_requirements", [node["id"]], "coverage_requirements 不得把 input-only token 作为 outcome/failure denominator。", {"requirement_ids": input_only_requirement_refs}))
                artifact_metadata_requirement_refs = sorted(
                    item["id"]
                    for item in node.get("coverage_requirements", [])
                    if item.get("category") == "metadata"
                    and _canonical_token(str(item.get("ref", "")).rsplit(".", 1)[-1]) in V4_OUTPUT_ONLY_ARTIFACT_TOKENS
                )
                if artifact_metadata_requirement_refs:
                    findings.append(_finding("TCO_V4_ARTIFACT_AS_METADATA_REQUIREMENT_REF", "error", "tco_model", f"/nodes/{node['id']}/coverage_requirements", [node["id"]], "P06 artifact output 不得作为 metadata coverage requirement。", {"requirement_ids": artifact_metadata_requirement_refs}))
                unresolved_values = [
                    *[value for item in node.get("inputs", []) for value in item.values()],
                    *[value for item in node.get("outputs", []) for value in item.values()],
                    *semantic.get("decision_inputs", []),
                    *semantic.get("decision_outputs", []),
                    *semantic.get("terminal_outcomes", []),
                    *semantic.get("failure_outcomes", []),
                    *semantic.get("decision_rules", []),
                    authority_owner,
                    *[value for item in node.get("metadata_requirements", []) for value in item.values()],
                    *[value for item in node.get("coverage_requirements", []) for value in item.values()],
                    *node.get("invariants", []),
                    *node.get("witness_artifact_refs", []),
                    *node.get("negative_fixture_refs", []),
                    *node.get("traceability_refs", []),
                ]
                if any("unresolved" in str(value) or "migration_unresolved" in str(value) for value in unresolved_values):
                    findings.append(_finding("TCO_V4_MIGRATION_UNRESOLVED_SEMANTICS", "error", "tco_model", f"/nodes/{node['id']}", [node["id"]], "迁移占位语义仍未解析，不能进入实现准备态。", {}))
            if node["node_kind"] == "policy":
                if any(key in node for key in ("inputs", "outputs", "decision_semantics")):
                    findings.append(_finding("TCO_V4_POLICY_HEAVY_CAPABILITY", "error", "tco_model", f"/nodes/{node['id']}", [node["id"]], "Policy 只能表达约束，不能冒充能力。", {}))
                bad_policy_requirements = sorted(
                    item["id"]
                    for item in node.get("coverage_requirements", [])
                    if item.get("category") != "metadata"
                    or not str(item.get("ref", "")).startswith("policy_requirements.")
                    or item.get("id") != f"metadata.{str(item.get('ref', '')).removeprefix('policy_requirements.')}"
                )
                if bad_policy_requirements:
                    findings.append(_finding("TCO_V4_POLICY_REQUIREMENT_REF_MISMATCH", "error", "tco_model", f"/nodes/{node['id']}/coverage_requirements", [node["id"]], "Policy coverage_requirements 只能是 metadata policy_requirements.<id> ref。", {"requirement_ids": bad_policy_requirements}))
        findings.extend(_audit_tsp_vocabulary(model, config))
        targets = [item["source_target_id"] for item in model["target_profiles"]]
        for target in sorted({value for value in targets if targets.count(value) > 1}):
            findings.append(_finding("TCO_V4_PROFILE_SOURCE_TARGET_DUPLICATE", "error", "tco_model", "/target_profiles", [target], "同一 source Target 不得创建 revision/view Profile。", {}))
        for profile_index, profile in enumerate(model["target_profiles"]):
            if re.search(r"(?:^|[-_\s])r\d{3,}|revision|\bview\b", profile["id"] + " " + profile["label"], re.IGNORECASE):
                findings.append(_finding("TCO_V4_PROFILE_REVISION_VIEW_NAME", "error", "tco_model", f"/target_profiles/{profile_index}", [profile["id"]], "Profile 不得使用 revision/view 命名；历史视图必须进入 lineage。", {}))
            for req_index, requirement in enumerate(profile["requirements"]):
                if any(nodes_by_id.get(node_id, {}).get("node_kind") == "mc" for node_id in requirement["node_ids"]):
                    findings.append(_finding("TCO_V4_REQUIREMENT_MAPS_MC", "error", "tco_model", f"/target_profiles/{profile_index}/requirements/{req_index}/node_ids", requirement["node_ids"], "Profile requirement 不得把 Mc 计入 Coverage 分母。", {}))
                if not requirement.get("witness_evidence_mapping"):
                    findings.append(_finding("TCO_V4_WITNESS_MAPPING_MISSING", "error", "tco_model", f"/target_profiles/{profile_index}/requirements/{req_index}/witness_evidence_mapping", requirement["node_ids"], "Profile requirement 必须显式映射 witness evidence。", {}))
                if not requirement.get("nf_evidence_mapping"):
                    findings.append(_finding("TCO_V4_NF_MAPPING_MISSING", "error", "tco_model", f"/target_profiles/{profile_index}/requirements/{req_index}/nf_evidence_mapping", requirement["node_ids"], "Profile requirement 必须显式映射 NF/negative evidence。", {}))
        findings.extend(_audit_tco_candidate_readiness_responsibility(model, code_prefix="TCO_V4"))
        findings.extend(_audit_tco_requirement_evidence_mapping(model, code_prefix="TCO_V4"))
        findings.extend(_audit_tco_requirement_semantic_compatibility(model, code_prefix="TCO_V4"))
        findings.extend(_audit_tco_provider_configuration_version_management(model, code_prefix="TCO_V4"))
        findings.extend(_audit_tco_mc_lifecycle_acceptance_coherence(model, code_prefix="TCO_V4"))
        findings.extend(_audit_tco_claim_exclusion_boundaries(model, code_prefix="TCO_V4"))
        if coverage_path is not None:
            coverage = load_json(coverage_path)
            try:
                validate_document("coverage_v3", coverage, coverage_path)
            except ContractError as exc:
                findings.append(
                    _finding(
                        "TCO_V4_COVERAGE_SCHEMA_INVALID",
                        "error",
                        "coverage",
                        "/",
                        [],
                        "v4 Coverage schema 校验失败；candidate audit 保留可见 finding，但不能把无效 schema 当作可用候选。",
                        {"error": str(exc)},
                    )
                )
            if coverage.get("candidate_status") != "candidate_not_approved":
                findings.append(_finding("TCO_V4_COVERAGE_CANDIDATE_STATUS_INVALID", "error", "coverage", "/candidate_status", [], "v4 coverage 必须保持 candidate_not_approved。", {}))
            record_ids = {record.get("tco_node_id") for record in coverage.get("records", []) if record.get("tco_node_id")}
            if coverage.get("coverage_mode") == "repo" and record_ids != counted_node_ids:
                findings.append(_finding("TCO_V4_COVERAGE_DENOMINATOR_DRIFT", "error", "coverage", "/records", sorted(record_ids), "v4 Coverage 必须精确覆盖所有 m1/policy，且不得覆盖 Mc。", {"missing": sorted(counted_node_ids - record_ids), "extra": sorted(record_ids - counted_node_ids)}))
            for record in coverage.get("records", []):
                node_id = str(record.get("tco_node_id", ""))
                node = nodes_by_id.get(node_id, {})
                expected = {item["id"] for item in node.get("coverage_requirements", [])}
                actual = {item.get("requirement_id") for item in record.get("requirement_coverage", []) if item.get("requirement_id")}
                if expected != actual:
                    findings.append(_finding("TCO_V4_REQUIREMENT_COVERAGE_DENOMINATOR_DRIFT", "error", "coverage", f"/records/{node_id}/requirement_coverage", [node_id], "v4 requirement_coverage 必须精确覆盖 node coverage_requirements。", {"missing": sorted(expected - actual), "extra": sorted(actual - expected)}))
            findings.extend(_audit_v4_orthogonal_state_axes(coverage, config, code_prefix="TCO_V4"))
            findings.extend(_audit_v4_breadth_configuration_comparability(coverage, config, code_prefix="TCO_V4"))
        return _report(report_kind="tco_v4_candidate_semantic_risk", artifact_kind="tco", artifact_digest=digest_value(model), base_digest=None, findings=findings)
    if model.get("schema") in {"kym_tco.tco_model.v2", "kym_tco.tco_model.v3"}:
        is_v3 = model.get("schema") == "kym_tco.tco_model.v3"
        schema_kind = "tco_model_v3" if is_v3 else "tco_model_v2"
        version_label = "v3" if is_v3 else "v2"
        validate_document(schema_kind, model, model_path)
        _require_product(model, product_id, f"TCO {version_label} model")
        findings: list[Finding] = []
        if model.get("candidate_status") != "candidate_not_approved":
            findings.append(_finding("TCO_V2_MODEL_CANDIDATE_STATUS_INVALID", "error", "tco_model", "/candidate_status", [], f"{version_label} model 必须保持 candidate_not_approved。", {}))
        nodes_by_id = {node["id"]: node for node in model["nodes"]}
        counted_node_ids = {node_id for node_id, node in nodes_by_id.items() if node["node_kind"] in {"m1", "policy"}}
        for node in model["nodes"]:
            if node["node_kind"] == "m1":
                required = {"capability_type", "inputs", "outputs", "decision_semantics", "invariants", "witness_artifact_refs", "negative_fixture_refs", "traceability_refs"}
                if not is_v3:
                    required.add("effect_status")
                missing = sorted(required - set(node))
                if missing or node["children"]:
                    findings.append(_finding("TCO_V2_M1_INCOMPLETE", "error", "tco_model", f"/nodes/{node['id']}", [node["id"]], "M1 必须具有完整独立能力字段且没有 children。", {"missing": missing, "children": node["children"]}))
                if is_v3 and "effect_status" in node:
                    findings.append(_finding("TCO_V3_M1_EFFECT_STATUS_FORBIDDEN", "error", "tco_model", f"/nodes/{node['id']}/effect_status", [node["id"]], "v3 M1 是 capability definition，不得携带 execution effect_status；请使用 tco_execution_result.v1。", {}))
                if not is_v3 and node.get("effect_status") not in {"not_executed", "incomplete", "attempted_failed", "achieved"}:
                    findings.append(_finding("TCO_EFFECT_STATUS_INVALID", "error", "tco_model", f"/nodes/{node['id']}/effect_status", [node["id"]], "effect status 必须使用四态词表且不得与 verdict 合并。", {}))
                if not is_v3 and node.get("effect_status") == "not_executed":
                    findings.append(_finding("TCO_V2_LEGACY_EFFECT_STATUS_MODEL_FIELD", "warning", "tco_model", f"/nodes/{node['id']}/effect_status", [node["id"]], "v2 legacy effect_status 是模型兼容字段，不得解读为某次 run 的 execution result。", {}))
                semantic = node.get("decision_semantics", {})
                if len(node.get("inputs", [])) > 2 and len(node.get("outputs", [])) > 2 and len(semantic.get("decision_rules", [])) > 2:
                    findings.append(_finding("TCO_V2_GOD_M1_REVIEW", "warning", "tco_model", f"/nodes/{node['id']}", [node["id"]], "M1 同时承担多输入、多输出、多决策规则，需复核是否应拆分。", {"input_count": len(node.get("inputs", [])), "output_count": len(node.get("outputs", [])), "decision_rule_count": len(semantic.get("decision_rules", []))}))
                text = " ".join(str(node.get(key, "")) for key in ("title", "purpose", "capability_type"))
                if ARTIFACT_ONLY_RE.search(text):
                    findings.append(_finding("TCO_V2_ARTIFACT_ONLY_M1_REVIEW", "warning", "tco_model", f"/nodes/{node['id']}", [node["id"]], "M1 看起来像产物/文档本身，需确认其表达的是可独立验证的能力。", {"text": text}))
                authority_owner = str(semantic.get("authority_owner", ""))
                if authority_owner and authority_owner != "unresolved" and MULTI_AUTHORITY_RE.search(authority_owner):
                    findings.append(_finding("TCO_V2_MULTI_AUTHORITY_OWNER", "error", "tco_model", f"/nodes/{node['id']}/decision_semantics/authority_owner", [node["id"]], "M1 只能有一个决策 authority owner。", {"authority_owner": authority_owner}))
                unresolved_values = [
                    *node.get("inputs", []),
                    *node.get("outputs", []),
                    *semantic.get("decision_input", []),
                    *semantic.get("decision_output", []),
                    *semantic.get("decision_rules", []),
                    *semantic.get("failure_modes", []),
                    authority_owner,
                    *node.get("invariants", []),
                    *node.get("witness_artifact_refs", []),
                    *node.get("negative_fixture_refs", []),
                    *node.get("traceability_refs", []),
                ]
                if any("unresolved" in str(value) or "migration_unresolved" in str(value) for value in unresolved_values):
                    findings.append(_finding("TCO_V2_MIGRATION_UNRESOLVED_SEMANTICS", "error", "tco_model", f"/nodes/{node['id']}", [node["id"]], "迁移占位语义仍未解析，不能进入实现准备态。", {}))
            if node["node_kind"] == "policy" and any(key in node for key in ("inputs", "outputs", "decision_semantics")):
                findings.append(_finding("TCO_POLICY_HEAVY_CAPABILITY", "error", "tco_model", f"/nodes/{node['id']}", [node["id"]], "Policy 只能表达约束，不能冒充能力。", {}))
        targets = [item["source_target_id"] for item in model["target_profiles"]]
        for target in sorted({value for value in targets if targets.count(value) > 1}):
            findings.append(_finding("TCO_PROFILE_SOURCE_TARGET_DUPLICATE", "error", "tco_model", "/target_profiles", [target], "同一 source Target 不得创建 revision/view Profile。", {}))
        for profile_index, profile in enumerate(model["target_profiles"]):
            if re.search(r"(?:^|[-_\s])r\d{3,}|revision|\bview\b", profile["id"] + " " + profile["label"], re.IGNORECASE):
                findings.append(_finding("TCO_V2_PROFILE_REVISION_VIEW_NAME", "error", "tco_model", f"/target_profiles/{profile_index}", [profile["id"]], "Profile 不得使用 revision/view 命名；历史视图必须进入 lineage。", {}))
            for req_index, requirement in enumerate(profile["requirements"]):
                if any(nodes_by_id.get(node_id, {}).get("node_kind") == "mc" for node_id in requirement["node_ids"]):
                    findings.append(_finding("TCO_V2_REQUIREMENT_MAPS_MC", "error", "tco_model", f"/target_profiles/{profile_index}/requirements/{req_index}/node_ids", requirement["node_ids"], "Profile requirement 不得把 Mc 计入 Coverage 分母。", {}))
                if not requirement.get("witness_evidence_mapping"):
                    findings.append(_finding("TCO_V2_WITNESS_MAPPING_MISSING", "error", "tco_model", f"/target_profiles/{profile_index}/requirements/{req_index}/witness_evidence_mapping", requirement["node_ids"], "Profile requirement 必须显式映射 witness evidence。", {}))
                if not requirement.get("nf_evidence_mapping"):
                    findings.append(_finding("TCO_V2_NF_MAPPING_MISSING", "error", "tco_model", f"/target_profiles/{profile_index}/requirements/{req_index}/nf_evidence_mapping", requirement["node_ids"], "Profile requirement 必须显式映射 NF/negative evidence。", {}))
                if any("unresolved" in str(value) or "migration_unresolved" in str(value) for value in requirement.get("missing_items", [])):
                    findings.append(_finding("TCO_V2_REQUIREMENT_UNRESOLVED_ITEMS", "error", "tco_model", f"/target_profiles/{profile_index}/requirements/{req_index}/missing_items", requirement["node_ids"], "Profile requirement 仍包含迁移未解析项。", {"missing_items": requirement.get("missing_items", [])}))
        legacy_code_prefix = "TCO_V3" if is_v3 else "TCO_V2"
        findings.extend(_audit_tco_candidate_readiness_responsibility(model, code_prefix=legacy_code_prefix))
        findings.extend(_audit_tco_requirement_evidence_mapping(model, code_prefix=legacy_code_prefix))
        findings.extend(_audit_tco_requirement_semantic_compatibility(model, code_prefix=legacy_code_prefix))
        findings.extend(_audit_tco_provider_configuration_version_management(model, code_prefix=legacy_code_prefix))
        findings.extend(_audit_tco_mc_lifecycle_acceptance_coherence(model, code_prefix=legacy_code_prefix))
        if coverage_path is not None:
            coverage = load_json(coverage_path); validate_document("coverage_v2", coverage, coverage_path)
            if coverage["candidate_status"] != "candidate_not_approved":
                findings.append(_finding("TCO_CANDIDATE_STATUS_INVALID", "error", "coverage", "/candidate_status", [], "v2 coverage 必须保持 candidate_not_approved。", {}))
            record_ids = {record["tco_node_id"] for record in coverage["records"]}
            if coverage["coverage_mode"] == "repo" and record_ids != counted_node_ids:
                findings.append(_finding("TCO_V2_COVERAGE_DENOMINATOR_DRIFT", "error", "coverage", "/records", sorted(record_ids), "v2 Coverage 必须精确覆盖所有 m1/policy，且不得覆盖 Mc。", {"missing": sorted(counted_node_ids - record_ids), "extra": sorted(record_ids - counted_node_ids)}))
        return _report(report_kind=f"tco_{version_label}_candidate_semantic_risk", artifact_kind="tco", artifact_digest=digest_value(model), base_digest=None, findings=findings)
    validate_document("tco_model", model, model_path)
    _require_product(model, product_id, "TCO model")
    base_model: dict[str, Any] | None = None
    if base_model_path is not None:
        base_model = load_json(base_model_path)
        validate_document("tco_model", base_model, base_model_path)
        _require_product(base_model, product_id, "TCO base model")
    coverage: dict[str, Any] | None = None
    if coverage_path is not None:
        coverage = load_json(coverage_path)
        validate_document("coverage", coverage, coverage_path)
        _require_product(coverage, product_id, "Coverage candidate")

    findings = _audit_tco_structure(model, base_model)
    findings.extend(_audit_tsp_vocabulary(model, config))
    if coverage is not None:
        findings.extend(_audit_coverage(coverage, config))
    artifact = {"model": model, "coverage": coverage} if coverage is not None else model
    return _report(
        report_kind="tco_candidate_semantic_risk",
        artifact_kind="tco",
        artifact_digest=digest_value(artifact),
        base_digest=digest_value(base_model) if base_model is not None else None,
        findings=findings,
    )


__all__ = ["audit_kym_candidate", "audit_tco_candidate"]
