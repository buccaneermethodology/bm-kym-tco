from __future__ import annotations

import re
from typing import Any


HUMAN_RE = re.compile(
    r"(用户|使用者|读者|团队|负责人|评审者|你|您|user|reader|team|operator|reviewer|owner|customer)",
    re.IGNORECASE,
)
TOPIC_CUE_RE = re.compile(
    r"(当|需要|想要|可以|能够|帮助|用来|when|need|want|can|help|use)",
    re.IGNORECASE,
)
PURPOSE_CUE_RE = re.compile(
    r"(让|避免|防止|减少|确保|便于|帮助|知道|判断|so that|avoid|prevent|reduce|ensure|help|understand|decide)",
    re.IGNORECASE,
)
INCLUDED_CUE_RE = re.compile(
    r"(会|可以|能够|包括|涵盖|提供|看到|得到|will|can|include|cover|provide|see|get)",
    re.IGNORECASE,
)
EXCLUDED_CUE_RE = re.compile(
    r"(不会|不能|不负责|不包含|不替代|仍需|仍然需要|will not|cannot|does not|exclude|still need)",
    re.IGNORECASE,
)


def _substantial(text: str) -> bool:
    normalized = " ".join(text.split())
    cjk_count = len(re.findall(r"[\u3400-\u9fff]", normalized))
    word_count = len(re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*", normalized))
    return cjk_count >= 14 or word_count >= 8 or (cjk_count >= 8 and word_count >= 3)


def _finding(code: str, node_id: str, path: str, message: str, text: str) -> dict[str, Any]:
    return {
        "code": code,
        "severity": "error",
        "node_id": node_id,
        "path": path,
        "message": message,
        "text": text,
    }


def audit_reader_language(model: dict[str, Any]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    m1_nodes = [node for node in model.get("nodes", []) if node.get("node_kind") == "m1"]
    for node in m1_nodes:
        node_id = str(node.get("id", "<missing-id>"))
        fields = (
            ("topic", str(node.get("topic", "")), TOPIC_CUE_RE, "使用场景或用户可获得的帮助"),
            ("purpose", str(node.get("purpose", "")), PURPOSE_CUE_RE, "用户收益或需要避免的风险"),
        )
        for field, value, cue_re, expected in fields:
            path = f"/nodes/{node_id}/{field}"
            if not _substantial(value):
                findings.append(_finding(f"TCO_M1_READER_{field.upper()}_TOO_THIN", node_id, path, f"{field} 不能只是短标签，必须用完整大白话解释{expected}。", value))
            if not HUMAN_RE.search(value) or not cue_re.search(value):
                findings.append(_finding(f"TCO_M1_READER_{field.upper()}_NOT_USER_ORIENTED", node_id, path, f"{field} 必须从人类用户角度说明{expected}。", value))

        boundary = node.get("scope", {}).get("boundary", {})
        for side, cue_re, expected in (
            ("included", INCLUDED_CUE_RE, "这项能力会替用户处理什么"),
            ("excluded", EXCLUDED_CUE_RE, "这项能力不会替用户做什么或仍需什么独立判断"),
        ):
            values = boundary.get(side, [])
            if not values:
                findings.append(_finding("TCO_M1_READER_SCOPE_ITEM_MISSING", node_id, f"/nodes/{node_id}/scope/boundary/{side}", f"Scope {side} 至少需要一条人类可读边界。", ""))
            for index, raw in enumerate(values):
                value = str(raw)
                path = f"/nodes/{node_id}/scope/boundary/{side}/{index}"
                if not _substantial(value):
                    findings.append(_finding("TCO_M1_READER_SCOPE_ITEM_TOO_THIN", node_id, path, "Scope 每一条都必须是完整的大白话说明，不能只是技术名词。", value))
                if not HUMAN_RE.search(value) or not cue_re.search(value):
                    findings.append(_finding("TCO_M1_READER_SCOPE_ITEM_NOT_USER_ORIENTED", node_id, path, expected + "。", value))

    return {
        "schema": "kym_tco.tco_reader_language_audit.v1",
        "candidate_only": True,
        "m1_count": len(m1_nodes),
        "error_count": len(findings),
        "findings": findings,
        "verdict": "pass" if not findings else "fail",
        "authority_note": "该审计只检查可观察的表达质量，不证明 TCO 业务语义正确，也不创建 approval 或 receipt。",
    }
