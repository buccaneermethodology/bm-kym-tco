#!/usr/bin/env python3
"""独立审计候选 KYM/TCO 的阅读表达；只输出结果，不写入候选。"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import re

AUTHORITY_NOTE = "仅检查可观察的阅读质量；pass 不表示业务语义通过，advisory 必须说明或人工复核；不创建 approval、receipt 或 Coverage 状态。"


def _norm(value):
    return " ".join(str(value or "").split())


def _key(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _report(findings, checks):
    errors = sum(f["severity"] == "error" for f in findings)
    advisories = len(findings) - errors
    return {"schema": "kym_tco.reader_quality_audit.v1", "candidate_only": True,
            "error_count": errors, "advisory_count": advisories,
            "verdict": "fail" if errors else "needs_review" if advisories else "pass",
            "findings": findings, "evaluated_checks": checks,
            "semantic_review_required": True, "authority_note": AUTHORITY_NOTE}


def audit(kym, model=None, coverage=None, detail=None):
    """只读公开字段；不导入仓库代码或 reader 私有实现。"""
    findings = []
    checks = ["kym_titles"]

    def add(code, severity, path, ids, message):
        findings.append({"code": code, "severity": severity, "path": path,
                         "subject_ids": ids, "message": message,
                         "resolution_required": True})

    if not isinstance(kym, dict) or not isinstance(kym.get("tree"), dict):
        raise ValueError("KYM 必须包含 tree 对象；请先运行候选 schema 检查")
    statements = {s["id"]: _norm(s.get("text")) for s in kym.get("statements", [])}
    prefixes = defaultdict(list)

    def walk(node, path):
        title = _norm(node.get("title"))
        texts = [statements[ref] for ref in node.get("statement_refs", []) if ref in statements]
        # 单独的长标题、相同短标题、自然等长标题都不是失败依据。
        if title in texts and (len(title) >= 48 or len(title.split()) >= 16):
            add("KYM_TITLE_REPEATS_BODY", "error", path + "/title", [node.get("id", "")],
                "长标题逐字复制所引用的完整正文；请保留完整正文，另写概括标题。")
        elif title and not re.search(r"[。！？.!?；;：:]$", title):
            if len(title) >= 12 or len(title.split()) >= 4:
                if any(t.startswith(title) and len(t) > len(title) * 1.25 for t in texts):
                    prefixes[len(title)].append((node.get("id", ""), path + "/title"))
        for index, child in enumerate(node.get("children", [])):
            walk(child, path + "/children/" + str(index))

    walk(kym["tree"], "/tree")
    for length, entries in sorted(prefixes.items()):
        if len(entries) >= 3:
            add("KYM_BATCH_TRUNCATED_TITLES", "error", "/tree", [x[0] for x in entries],
                f"至少三个标题都是所引用正文的等长 {length} 字符半截前缀；请逐项概括，不要批量切片。")

    if detail is not None and (not isinstance(detail, dict) or not isinstance(detail.get("capabilities"), dict)):
        raise ValueError("detail 必须包含 capabilities 对象")
    if model is None:
        if coverage is not None:
            raise ValueError("审计 TCO Coverage 必须同时提供 model")
        if detail is None:
            return _report(findings, checks)
        by_id = {}
        def collect(node):
            by_id[node.get("id")] = node
            for child in node.get("children", []):
                collect(child)
        collect(kym["tree"])
        nodes = []
        for key in detail["capabilities"]:
            if key in by_id:
                nodes.append(by_id[key])
            else:
                add("KYM_DETAIL_UNKNOWN_NODE", "error", "/capabilities/" + key, [key],
                    "KYM 阅读详细内容指向不存在的树节点。")
    else:
        if not isinstance(model, dict) or not isinstance(model.get("nodes"), list):
            raise ValueError("model 必须包含 nodes 数组")
        nodes = [n for n in model["nodes"] if n.get("node_kind") == "m1"]
    records = {}
    if coverage is not None:
        if not isinstance(coverage, dict) or not isinstance(coverage.get("records"), list):
            raise ValueError("coverage 必须包含 records 数组")
        records = {r["tco_node_id"]: r for r in coverage["records"]}
        checks.append("coverage_behavior_explanation")
    coverage_evaluated = coverage is not None and coverage.get("coverage_mode") != "not_evaluated"
    caps = detail.get("capabilities", {}) if detail is not None else {}
    if detail is not None:
        checks += ["assessment_pass_condition", "shared_evidence_explanation"]
        if coverage_evaluated:
            checks.append("mandatory_assessment")
        else:
            add("TCO_COVERAGE_NOT_EVALUATED", "advisory", "/capabilities", [],
                "未提供或未评估 Coverage：mandatory 行为对覆盖分母的完整性尚未评估；允许明确未映射的待评估行为，不推断覆盖状态。")
    else:
        add("TCO_DETAIL_NOT_EVALUATED", "advisory", "/capabilities", [],
            "没有 detail：逐行为证据适用性和推进条件尚未评估；不能从无仓库或缺证据推导 not_started。")

    boundaries = defaultdict(list)
    mechanical = []
    evidence_uses = defaultdict(list)
    generic_acceptance = {"通过", "测试通过", "验证通过", "完成", "确认完成", "待确认", "待定", "待补充",
                          "通过测试", "运行测试", "测试成功", "满足要求", "符合预期", "pass", "tests pass", "tbd", "todo", "done", "verified"}
    for node in nodes:
        nid = node["id"]
        scope = node.get("scope", {})
        boundary = scope.get("boundary", {}) if isinstance(scope, dict) else {"included": list(scope), "excluded": []}
        if boundary.get("included") or boundary.get("excluded"):
            boundaries[_key(boundary)].append(nid)
        requirements = {r["id"]: r for r in node.get("coverage_requirements", [])}
        if isinstance(scope, list) and not requirements:
            # Legacy Scope strings are compatibility refs, not invented v4 requirements.
            requirements = {value: {"id": value, "category": "legacy_scope"} for value in scope}
        mandatory = set(node.get("mandatory_coverage_requirement_ids", []))
        mandatory |= {rid for rid, req in requirements.items() if req.get("mandatory") is True}
        assessments = caps.get(nid, {}).get("assessment", [])
        covered = {rid for row in assessments for rid in row.get("requirement_ids", [])}
        if detail is not None and coverage_evaluated and mandatory - covered:
            add("TCO_MANDATORY_ASSESSMENT_MISSING", "error", "/capabilities/" + nid + "/assessment", [nid],
                "逐行为判断遗漏 mandatory requirement：" + ", ".join(sorted(mandatory - covered)))
        for index, row in enumerate(assessments):
            path = f"/capabilities/{nid}/assessment/{index}"
            for rid in row.get("requirement_ids", []):
                if rid not in requirements:
                    add("TCO_ASSESSMENT_UNKNOWN_REQUIREMENT", "error", path, [nid, rid],
                        "行为映射引用本 M 不存在的 requirement；应绑定真实要求，未映射行为用空 requirement_ids 明示。")
            if not row.get("requirement_ids") and not coverage_evaluated and row.get("gap_kind") not in {"not_evaluated", "business_question"}:
                add("TCO_UNMAPPED_BEHAVIOR_UNEXPLAINED", "error", path + "/requirement_ids", [nid, row.get("id", "")],
                    "无 Coverage 的未映射行为应明示 not_evaluated 或 business_question，并补充证据边界与下一步。")
            acceptance = _norm(row.get("acceptance"))
            action = _norm(row.get("next_action"))
            if row.get("gap_kind") != "none" and (not acceptance or acceptance.lower() in generic_acceptance or acceptance == action):
                add("TCO_ACTION_WITHOUT_PASS_CONDITION", "error", path + "/acceptance", [nid, row.get("id", "")],
                    "推进动作没有独立的具体通过条件；请描述可观察结果，不能只重复动作或写“测试通过”。")
            refs = tuple(sorted(row.get("evidence_refs", [])))
            if refs:
                evidence_uses[refs].append((nid, row))
        req_rows = records.get(nid, {}).get("requirement_coverage", [])
        # I/O 语义来自 requirement category，绝不匹配行标题关键词或固定行数。
        categories = {requirements[r["requirement_id"]].get("category") for r in req_rows if r.get("requirement_id") in requirements}
        all_known = bool(req_rows) and all(r.get("requirement_id") in requirements for r in req_rows)
        same_support = len({_key([_norm(r.get("notes")), sorted(r.get("evidence_refs", []))]) for r in req_rows}) == 1
        explanations = {_key([_norm(r.get("evidence_summary")), _norm(r.get("applicability"))]) for r in assessments}
        assessed_ids = {rid for row in assessments for rid in row.get("requirement_ids", [])}
        notes = {_norm(r.get("notes")) for r in req_rows}
        # 一条可观察行为可映射多个要求；不强制每个 I/O 各填一行。
        per_behavior = (bool(assessments) and len(explanations) == len(assessments)
                        and all(_norm(r.get("behavior")) and _norm(r.get("evidence_summary"))
                                and _norm(r.get("applicability"))
                                and _norm(r.get("evidence_summary")) not in notes for r in assessments)
                        and {r.get("requirement_id") for r in req_rows} <= assessed_ids)
        if all_known and categories == {"input", "output"} and same_support and not per_behavior:
            mechanical.append(nid)
    if len(nodes) >= 2 and len(mechanical) == len(nodes):
        add("TCO_MECHANICAL_IO_COVERAGE", "error", "/records", mechanical,
            "全部 M 仅用 input/output 要求配同一 notes/evidence，且无逐行为不同支持点；请解释每行为证据与适用范围。共享证据本身允许。")
    for ids in boundaries.values():
        if len(ids) > 1:
            add("TCO_SHARED_LOCAL_BOUNDARY", "advisory", "/nodes", ids,
                "多个 M 使用相同局部边界；请说明这是合法共享，或改写成本 M 的具体职责。重复本身不判错。")
    for uses in evidence_uses.values():
        explanations = {_key([_norm(r.get("evidence_summary")), _norm(r.get("applicability"))]) for _, r in uses}
        if len(uses) > 1 and len(explanations) == 1:
            add("TCO_SHARED_EVIDENCE_EXPLANATION", "advisory", "/capabilities", sorted({n for n, _ in uses}),
                "共享证据的支持点与适用性说明完全相同；请解释各行为如何被支持。允许同一证据支持多个行为，不据此改变覆盖状态。")
    checks.append("local_boundary_specificity")
    return _report(findings, checks)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kym", type=Path, required=True)
    for name in ("model", "coverage", "detail"):
        parser.add_argument("--" + name, type=Path)
    args = parser.parse_args()
    try:
        values = [json.loads(p.read_text(encoding="utf-8")) if p else None
                  for p in (args.kym, args.model, args.coverage, args.detail)]
        report = audit(*values)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        report = _report([{"code": "READER_QUALITY_INPUT_ERROR", "severity": "error", "path": "/",
                           "subject_ids": [], "message": "输入无法审计：" + str(exc), "resolution_required": True}], [])
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if report["error_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
