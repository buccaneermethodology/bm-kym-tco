from __future__ import annotations

import re
from typing import Any

from .common import ContractError, digest_bytes


FINDING_CODE = "KYM_READER_MARKDOWN_NONCANONICAL"


def audit_reader_markdown(markdown: str, *, expected: str | None = None) -> dict[str, Any]:
    """检查确定性 KYM reader 的 CommonMark 子集与逐字重渲染一致性。"""

    issues: list[dict[str, Any]] = []
    lines = markdown.splitlines()

    def add(kind: str, line: int | None, detail: str) -> None:
        issues.append({"kind": kind, "line": line, "detail": detail})

    if "\u00a0" in markdown:
        add("nbsp", None, "reader 含 NBSP，Markdown 层级必须只使用 ASCII 空格")
    if "\t" in markdown:
        add("tab", None, "reader 含 Tab，列表缩进必须是两个 ASCII 空格的整数倍")
    if expected is not None and markdown != expected:
        add("rerender_byte_drift", None, "reader bytes 与同一 JSON 的确定性重渲染结果不一致")

    content_depths: list[tuple[int, int]] = []
    in_content = False
    for line_number, line in enumerate(lines, 1):
        if line.endswith("  "):
            add("trailing_double_space", line_number, "行尾双空格不属于 canonical reader")
        if re.match(r"^\s*\*\*#{1,6}", line):
            add("bold_wrapped_heading", line_number, "标题不得包裹在加粗标记中")
        if line.lstrip(" ").startswith("#"):
            if not re.fullmatch(r"#{1,3} [^ ].*", line):
                add("heading", line_number, "标题必须是无缩进的裸 #、## 或 ### ATX heading")
        if line == "## KYM 内容":
            in_content = True
            continue
        if in_content and line == "## Target Profiles":
            in_content = False
        list_match = re.match(r"^( *)-(.*)$", line)
        if list_match:
            indent = len(list_match.group(1))
            suffix = list_match.group(2)
            if indent % 2:
                add("list_indent", line_number, "列表缩进必须是两个 ASCII 空格的整数倍")
            if not suffix.startswith(" ") or suffix.startswith("  "):
                add("list_marker", line_number, "列表标记必须严格使用 '- '")
            if in_content:
                content_depths.append((line_number, indent // 2))

    if not lines or not lines[0].startswith("# KYM："):
        add("document_heading", 1 if lines else None, "reader 第一行必须是 '# KYM：<product> (<revision>)'")
    for required in ("## 来源清单", "## KYM 内容"):
        if required not in lines:
            add("required_heading", None, f"reader 缺少固定标题 {required!r}")
    if content_depths:
        if content_depths[0][1] != 0:
            add("tree_root_indent", content_depths[0][0], "KYM tree 根节点必须从零缩进开始")
        previous = content_depths[0][1]
        for line_number, depth in content_depths[1:]:
            if depth > previous + 1:
                add("tree_depth_jump", line_number, "列表层级不得一次跨越一个以上深度")
            previous = depth
    else:
        add("tree_missing", None, "KYM 内容必须包含可解析的列表树")

    findings = []
    if issues:
        findings.append(
            {
                "code": FINDING_CODE,
                "severity": "error",
                "artifact_kind": "kym_reader_markdown",
                "path": "/reader",
                "subject_ids": ["reader"],
                "message": "reader Markdown 不符合确定性 CommonMark 子集或逐字重渲染合同。",
                "evidence": {"issues": issues},
                "confidence": "high",
                "resolution_required": True,
                "current_node_ids": [],
                "suggested_node_ids": [],
            }
        )
    return {
        "report_kind": "kym_reader_markdown_audit",
        "candidate_only": True,
        "artifact_kind": "kym_reader_markdown",
        "artifact_digest": digest_bytes(markdown.encode("utf-8")),
        "error_count": len(findings),
        "warning_count": 0,
        "findings": findings,
        "authority_note": "该确定性格式检查不证明 KYM 语义正确，也不创建 approval 或 receipt。",
    }


def validate_reader_markdown(markdown: str, *, expected: str | None = None) -> None:
    report = audit_reader_markdown(markdown, expected=expected)
    if report["error_count"]:
        issue_kinds = sorted(
            {issue["kind"] for item in report["findings"] for issue in item["evidence"]["issues"]}
        )
        raise ContractError(f"{FINDING_CODE}: {', '.join(issue_kinds)}")


def repair_presentation_markdown(
    markdown: str,
    *,
    remove_titles: set[str] | None = None,
    title_replacements: dict[str, str] | None = None,
    presentation_note: str | None = None,
) -> str:
    """只修复展示字节与已确认污染分支，不创建 candidate 身份。"""

    remove_titles = remove_titles or set()
    title_replacements = title_replacements or {}
    normalized = markdown.replace("\u00a0", " ")
    output: list[str] = []
    removing_depth: int | None = None
    for raw_line in normalized.splitlines():
        line = raw_line.rstrip()
        heading = re.fullmatch(r"\*\*(#{1,3} .+)\*\*", line)
        if heading:
            line = heading.group(1)
        item = re.match(r"^( *)- (.*)$", line)
        if removing_depth is not None:
            if not line:
                continue
            if item is not None and len(item.group(1)) > removing_depth:
                continue
            removing_depth = None
        if item is not None:
            indent, title = len(item.group(1)), item.group(2)
            if title in remove_titles:
                removing_depth = indent
                continue
            replacement = title_replacements.get(title)
            if replacement is not None:
                line = f"{' ' * indent}- {replacement}"
        output.append(line)

    if presentation_note:
        insert_at = 1 if output else 0
        output[insert_at:insert_at] = ["", f"> {presentation_note}"]
    repaired = "\n".join(output).rstrip() + "\n"
    validate_reader_markdown(repaired)
    return repaired
