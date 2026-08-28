from __future__ import annotations

import copy
import re
import subprocess
from pathlib import Path
from typing import Any

from .approval_authorization import validate_system_authorization
from .common import (
    ContractError,
    current_user,
    digest_bytes,
    digest_value,
    load_json,
    next_revision,
    project_root,
    require_id,
    resolved_within,
    utc_now,
    write_json,
)
from .validation import validate_document
from .reader_markdown import validate_reader_markdown
from .workspace import load_config


SPEAKER_RE = re.compile(r"^#{1,6}\s*(user|assistant|chatgpt|system|developer)\s*$", re.IGNORECASE)
LEGACY_TOP_LEVEL_RE = re.compile(
    r"^-\s+-\s+([123])\s+(Customer|Product|Project|KYC|KYProd|KYProj)\b"
)


KYM_TEMPLATE = (
    (
        "customer",
        "Customer",
        (
            ("customers", "Customers"),
            ("users", "Users"),
            ("stakeholders", "Stakeholders"),
        ),
    ),
    (
        "product",
        "Product",
        (
            ("info", "Info"),
            ("naming_conventions", "Naming Conventions"),
            ("scope", "Scope"),
            ("exclusions", "Exclusions"),
            ("business_rules", "Business Rules"),
            ("documents", "Documents"),
            ("problems_unsolved", "Problems Unsolved"),
            ("standards", "Standards"),
        ),
    ),
    (
        "project",
        "Project",
        (
            ("deliverables", "Deliverables"),
            ("equipments_and_tools", "Equipments and Tools"),
            ("schedule", "Schedule"),
            ("team", "Team"),
        ),
    ),
)


def _slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    value = value[:40] or "source"
    return value if value[0].isalpha() else f"source-{value}"


def _source_turns(lines: list[str]) -> list[dict[str, Any]]:
    ranges: list[tuple[int, int]] = []
    start = 1
    for index, line in enumerate(lines, start=1):
        if index > start and SPEAKER_RE.match(line.strip()):
            ranges.append((start, index - 1))
            start = index
    if lines:
        ranges.append((start, len(lines)))
    turns = []
    for index, (turn_start, turn_end) in enumerate(ranges, start=1):
        match = SPEAKER_RE.match(lines[turn_start - 1].strip())
        turns.append(
            {
                "id": f"t{index:04d}",
                "speaker": match.group(1).lower() if match else "unknown",
                "start_line": turn_start,
                "end_line": turn_end,
            }
        )
    return turns


def _chunk_turns(lines: list[str], turns: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
    oversized = [index for index, line in enumerate(lines, start=1) if len(line) + 1 > max_chars]
    if oversized:
        raise ContractError(
            f"source 存在超过单 chunk 上限的单行；请先换行规范化: lines={oversized[:10]}"
        )
    chunks: list[dict[str, Any]] = []
    current_start: int | None = None
    current_end: int | None = None
    current_chars = 0
    for turn in turns:
        turn_start = turn["start_line"]
        turn_end = turn["end_line"]
        turn_chars = sum(len(lines[i - 1]) + 1 for i in range(turn_start, turn_end + 1))
        if current_start is not None and current_chars + turn_chars > max_chars:
            chunks.append({"start_line": current_start, "end_line": current_end, "char_count": current_chars})
            current_start = None
            current_chars = 0
        if current_start is None:
            current_start = turn_start
        current_end = turn_end
        current_chars += turn_chars
        while current_chars > max_chars and current_start is not None and current_end is not None:
            split_end = current_start
            split_chars = 0
            while split_end <= current_end:
                next_chars = len(lines[split_end - 1]) + 1
                if split_chars and split_chars + next_chars > max_chars:
                    break
                split_chars += next_chars
                split_end += 1
            chunks.append({"start_line": current_start, "end_line": split_end - 1, "char_count": split_chars})
            current_start = split_end
            current_chars -= split_chars
    if current_start is not None and current_end is not None:
        chunks.append({"start_line": current_start, "end_line": current_end, "char_count": current_chars})
    return chunks


def ingest_source(workspace: Path, product_id: str, source_path: Path) -> Path:
    root = project_root(workspace, product_id)
    config = load_config(root)
    raw = source_path.read_bytes()
    text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ContractError("source 为空，不能 ingest")
    if not text.endswith("\n"):
        text += "\n"
    normalized = text.encode("utf-8")
    digest = digest_bytes(normalized)
    source_id = f"{_slug(source_path.stem)}-{digest.split(':')[1][:12]}"
    snapshot = root / "sources" / "snapshots" / f"{source_id}{source_path.suffix or '.txt'}"
    if snapshot.exists() and digest_bytes(snapshot.read_bytes()) != digest:
        raise ContractError(f"source snapshot identity 冲突: {snapshot}")
    snapshot.write_bytes(normalized)
    lines = text.splitlines()
    turns = _source_turns(lines)
    chunks = _chunk_turns(lines, turns, int(config["source_chunk_max_chars"]))
    for index, chunk in enumerate(chunks, start=1):
        chunk_id = f"{source_id}-c{index:04d}"
        chunk["id"] = chunk_id
        chunk_text = "\n".join(lines[chunk["start_line"] - 1 : chunk["end_line"]]) + "\n"
        chunk_path = root / "sources" / "chunks" / f"{chunk_id}.txt"
        chunk_path.write_text(chunk_text, encoding="utf-8")
        chunk["ref"] = str(chunk_path.relative_to(root))
        chunk["digest"] = digest_bytes(chunk_text.encode("utf-8"))
    manifest = {
        "schema": "kym_tco.source_manifest.v1",
        "source_id": source_id,
        "original_name": source_path.name,
        "snapshot_ref": str(snapshot.relative_to(root)),
        "digest": digest,
        "line_count": len(lines),
        "ingested_at": utc_now(),
        "turns": turns,
        "chunks": chunks,
    }
    manifest_path = root / "sources" / f"{source_id}.manifest.json"
    write_json(manifest_path, manifest)
    return manifest_path


def ingest_repo(workspace: Path, product_id: str, configured_path: Path) -> Path:
    """Snapshot configured repo evidence without allowing root or symlink escape."""
    root = project_root(workspace, product_id)
    config = load_config(root)
    if configured_path.is_absolute() or ".." in configured_path.parts:
        raise ContractError("repo evidence path 必须是 workspace 内相对路径")
    requested = resolved_within(workspace, configured_path.as_posix(), "repo evidence path")
    allowed_roots = [resolved_within(workspace, item, "evidence root") for item in config["evidence_roots"]]
    if not allowed_roots:
        raise ContractError("未配置 evidence_roots；repo ingestion 退化为 source-only")
    allowed_root = next((item for item in allowed_roots if requested == item or item in requested.parents), None)
    if allowed_root is None:
        raise ContractError("repo evidence path 不在配置 evidence_roots 内")
    if not requested.is_file() or requested.is_symlink():
        raise ContractError("repo evidence 必须是非 symlink 的普通文件")
    raw = requested.read_bytes()
    try:
        body = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError("repo evidence 仅支持 UTF-8 文本文件") from exc
    relative = requested.relative_to(allowed_root).as_posix()
    root_ref = allowed_root.relative_to(workspace.resolve()).as_posix()
    def git(*args: str) -> str | None:
        result = subprocess.run(["git", "-C", str(allowed_root), *args], capture_output=True, text=True, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    head = git("rev-parse", "HEAD")
    dirty = bool(git("status", "--porcelain")) if head else True
    text = f"# Repo evidence: {root_ref}/{relative}\n{body.replace(chr(13)+chr(10), chr(10)).replace(chr(13), chr(10))}"
    if not text.endswith("\n"):
        text += "\n"
    digest = digest_bytes(text.encode("utf-8"))
    source_id = f"repo-{_slug(relative)}-{digest.split(':')[1][:12]}"
    snapshot = root / "sources" / "snapshots" / f"{source_id}.txt"
    snapshot.write_text(text, encoding="utf-8")
    lines = text.splitlines(); turns = [{"id": "t0001", "speaker": "unknown", "start_line": 1, "end_line": len(lines)}]
    chunks = _chunk_turns(lines, turns, int(config["source_chunk_max_chars"]))
    for index, chunk in enumerate(chunks, 1):
        chunk_id = f"{source_id}-c{index:04d}"; chunk["id"] = chunk_id
        chunk_text = "\n".join(lines[chunk["start_line"] - 1:chunk["end_line"]]) + "\n"
        path = root / "sources" / "chunks" / f"{chunk_id}.txt"; path.write_text(chunk_text, encoding="utf-8")
        chunk["ref"] = str(path.relative_to(root)); chunk["digest"] = digest_bytes(chunk_text.encode("utf-8"))
    manifest = {"schema": "kym_tco.source_manifest.v2", "source_id": source_id, "original_name": requested.name,
        "snapshot_ref": str(snapshot.relative_to(root)), "digest": digest, "line_count": len(lines), "ingested_at": utc_now(), "turns": turns, "chunks": chunks,
        "repo_evidence": {"root_alias": root_ref, "root_ref": root_ref, "git_head": head, "dirty_status": dirty,
          "snapshot_kind": "git_head_plus_worktree" if dirty else "git_head", "files": [{"relative_path": relative, "file_type": requested.suffix.lstrip(".") or "text", "digest": digest_bytes(raw)}]}}
    path = root / "sources" / f"{source_id}.manifest.json"; write_json(path, manifest); return path


def inspect_legacy_markdown(path: Path) -> dict[str, Any]:
    lines = path.read_text(encoding="utf-8").splitlines()
    findings: list[dict[str, Any]] = []
    in_kyproj = False
    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        malformed = LEGACY_TOP_LEVEL_RE.match(line)
        if malformed:
            findings.append(
                {
                    "kind": "malformed_top_level_bullet",
                    "line": line_no,
                    "text": stripped,
                    "severity": "error",
                }
            )
        if "Scope & Exclusives" in line:
            findings.append(
                {
                    "kind": "taxonomy_label_drift",
                    "line": line_no,
                    "text": stripped,
                    "severity": "error",
                }
            )
        if re.search(r"\b3\s+(?:Project|KYProj)\b", line):
            in_kyproj = True
            findings.append(
                {
                    "kind": "unprovenanced_kyproj_section",
                    "line": line_no,
                    "text": stripped,
                    "severity": "error",
                }
            )
        if in_kyproj and ("/Users/" in line or "当前工作区" in line or "当前可见 CLI" in line):
            findings.append(
                {
                    "kind": "repo_enrichment_requires_source_snapshot",
                    "line": line_no,
                    "text": stripped,
                    "severity": "warning",
                }
            )
        if in_kyproj and (re.search(r"\b20\d{6}\b", line) or "当前" in line):
            findings.append(
                {
                    "kind": "temporal_fact_requires_revalidation",
                    "line": line_no,
                    "text": stripped,
                    "severity": "warning",
                }
            )
    return {
        "schema": "kym_tco.legacy_markdown_inspection.v1",
        "source": str(path),
        "finding_count": len(findings),
        "findings": findings,
        "authority_note": "该报告只识别结构/provenance/时效性风险候选，不自动修复或批准 KYM。",
    }


def _walk_tree(node: dict[str, Any], parent: str | None = None) -> list[tuple[dict[str, Any], str | None]]:
    rows = [(node, parent)]
    for child in node.get("children", []):
        rows.extend(_walk_tree(child, node["id"]))
    return rows


def _validate_kym_template(tree: dict[str, Any]) -> None:
    if (tree.get("id"), tree.get("title")) != ("kym", "KYM"):
        raise ContractError("KYM root 必须原样使用 id=kym、title=KYM")
    actual_sections = [(node.get("id"), node.get("title")) for node in tree.get("children", [])]
    expected_sections = [(node_id, title) for node_id, title, _children in KYM_TEMPLATE]
    if actual_sections != expected_sections:
        raise ContractError(
            f"KYM 一级节点必须按模板原样使用且顺序固定: expected={expected_sections}, actual={actual_sections}"
        )
    for section, (section_id, _title, expected_children) in zip(tree["children"], KYM_TEMPLATE):
        actual_children = [
            (node.get("id"), node.get("title")) for node in section.get("children", [])
        ]
        expected = list(expected_children)
        if actual_children != expected:
            raise ContractError(
                f"KYM 二级节点必须按模板原样使用且顺序固定: section={section_id}, "
                f"expected={expected}, actual={actual_children}"
            )


def semantic_validate_kym(
    document: dict[str, Any], root: Path, *, enforce_template: bool = True
) -> None:
    validate_document("kym", document)
    if document["product_id"] != root.name:
        raise ContractError("KYM product_id 与 workspace product 不一致")
    config = load_config(root)
    manifest_by_source: dict[str, dict[str, Any]] = {}
    for manifest_ref in document["source_manifest_refs"]:
        manifest_path = resolved_within(root, manifest_ref, "source_manifest_ref")
        manifest = load_json(manifest_path)
        validate_document("source_manifest_v2" if manifest.get("schema") == "kym_tco.source_manifest.v2" else "source_manifest", manifest, manifest_path)
        source_id = manifest.get("source_id")
        require_id(source_id, "source_id")
        if source_id in manifest_by_source:
            raise ContractError(f"source_manifest_refs 重复 source_id: {source_id}")
        snapshot_path = resolved_within(root, manifest["snapshot_ref"], "source snapshot")
        snapshot_bytes = snapshot_path.read_bytes()
        if digest_bytes(snapshot_bytes) != manifest["digest"]:
            raise ContractError(f"source snapshot digest 已漂移: {snapshot_path}")
        snapshot_lines = snapshot_bytes.decode("utf-8").splitlines()
        if manifest["line_count"] != len(snapshot_lines):
            raise ContractError(f"source manifest line_count 与 snapshot 不一致: {manifest_path}")
        expected_start = 1
        for index, turn in enumerate(manifest["turns"], start=1):
            if turn["id"] != f"t{index:04d}" or turn["start_line"] != expected_start:
                raise ContractError(f"source turns 必须稳定、连续: {manifest_path}")
            if turn["end_line"] < turn["start_line"] or turn["end_line"] > len(snapshot_lines):
                raise ContractError(f"source turn line range 非法: {turn}")
            expected_start = turn["end_line"] + 1
        if expected_start != len(snapshot_lines) + 1:
            raise ContractError(f"source turns 未完整覆盖 snapshot: {manifest_path}")
        expected_start = 1
        for chunk in manifest["chunks"]:
            chunk_path = resolved_within(root, chunk["ref"], "source chunk")
            chunk_bytes = chunk_path.read_bytes()
            if digest_bytes(chunk_bytes) != chunk["digest"]:
                raise ContractError(f"source chunk digest 已漂移: {chunk_path}")
            if chunk["start_line"] != expected_start or chunk["end_line"] < chunk["start_line"]:
                raise ContractError(f"source chunks 必须连续: {manifest_path}")
            expected_bytes = (
                "\n".join(snapshot_lines[chunk["start_line"] - 1 : chunk["end_line"]]) + "\n"
            ).encode("utf-8")
            if chunk_bytes != expected_bytes or chunk["char_count"] != len(expected_bytes.decode("utf-8")):
                raise ContractError(f"source chunk 与 snapshot slice 不一致: {chunk_path}")
            if chunk["char_count"] > int(config["source_chunk_max_chars"]):
                raise ContractError(f"source chunk 超过配置上限: {chunk_path}")
            expected_start = chunk["end_line"] + 1
        if expected_start != len(snapshot_lines) + 1:
            raise ContractError(f"source chunks 未完整覆盖 snapshot: {manifest_path}")
        manifest_by_source[source_id] = manifest

    if enforce_template:
        _validate_kym_template(document["tree"])
    statements = document["statements"]
    statement_by_id = {item["id"]: item for item in statements}
    if len(statement_by_id) != len(statements):
        raise ContractError("statement id 必须唯一")
    tree_rows = _walk_tree(document["tree"])
    tree_by_id = {item["id"]: item for item, _parent in tree_rows}
    if len(tree_by_id) != len(tree_rows):
        raise ContractError("tree node id 必须唯一")
    for node, _parent in tree_rows:
        missing = sorted(set(node["statement_refs"]) - set(statement_by_id))
        if missing:
            raise ContractError(f"tree node {node['id']} 引用不存在 statements: {missing}")
        asymmetric = sorted(
            statement_id
            for statement_id in set(node["statement_refs"])
            if node["id"] not in statement_by_id[statement_id]["tree_node_ids"]
        )
        if asymmetric:
            raise ContractError(
                f"tree node 与 statement 的双向引用不一致: node={node['id']}, "
                f"statements={asymmetric}"
            )
    for item in statements:
        missing_nodes = sorted(set(item["tree_node_ids"]) - set(tree_by_id))
        if missing_nodes:
            raise ContractError(f"statement {item['id']} 引用不存在 tree nodes: {missing_nodes}")
        asymmetric_nodes = sorted(
            node_id
            for node_id in set(item["tree_node_ids"])
            if item["id"] not in tree_by_id[node_id]["statement_refs"]
        )
        if asymmetric_nodes:
            raise ContractError(
                f"statement 与 tree node 的双向引用不一致: statement={item['id']}, "
                f"nodes={asymmetric_nodes}"
            )
        if not item["source_refs"]:
            raise ContractError(f"statement 缺 source_refs，不能进入可批准 KYM: {item['id']}")
        for ref in item["source_refs"]:
            if ref["source_id"] not in manifest_by_source:
                raise ContractError(
                    f"statement {item['id']} 引用了未登记 source manifest: {ref['source_id']}"
                )
            manifest = manifest_by_source[ref["source_id"]]
            if manifest.get("schema") == "kym_tco.source_manifest.v2" and "repo_evidence" in manifest and item["support_status"] != "externally_enriched":
                raise ContractError(f"repo evidence statement 必须标记 externally_enriched: {item['id']}")
            if ref["start_line"] > ref["end_line"] or ref["end_line"] > manifest["line_count"]:
                raise ContractError(f"statement {item['id']} source line range 非法: {ref}")
            turns = {turn["id"]: turn for turn in manifest["turns"]}
            missing_turns = sorted(set(ref["turn_ids"]) - set(turns))
            if missing_turns:
                raise ContractError(f"statement {item['id']} 引用不存在 source turns: {missing_turns}")
            selected_turns = [turns[turn_id] for turn_id in ref["turn_ids"]]
            if not all(
                turn["start_line"] <= ref["end_line"] and ref["start_line"] <= turn["end_line"]
                for turn in selected_turns
            ):
                raise ContractError(f"statement {item['id']} turn_ids 与 line range 不相交: {ref}")
            overlapping_turns = {
                turn_id
                for turn_id, turn in turns.items()
                if turn["start_line"] <= ref["end_line"] and ref["start_line"] <= turn["end_line"]
            }
            if set(ref["turn_ids"]) != overlapping_turns:
                raise ContractError(f"statement {item['id']} turn_ids 必须精确覆盖 line range: {ref}")
    target_ids = [item["id"] for item in document["target_profiles"]]
    if len(target_ids) != len(set(target_ids)):
        raise ContractError("target profile id 必须唯一")
    for target in document["target_profiles"]:
        validate_document("target_fragment", target)
        missing = sorted(set(target["requirement_refs"]) - set(statement_by_id))
        if missing:
            raise ContractError(f"target {target['id']} 引用不存在 statements: {missing}")
    if document["target_profiles"]:
        required_path = {"product", "scope", "target_profiles"}
        if not required_path.issubset(tree_by_id):
            raise ContractError("存在 Target 时 tree 必须包含 product/scope/target_profiles")
    referenced_findings = set()
    for collection in (document["conflicts"], document["unresolved_questions"]):
        for finding in collection:
            referenced_findings.update(finding["statement_refs"])
    missing_findings = referenced_findings - set(statement_by_id)
    if missing_findings:
        raise ContractError(f"finding 引用不存在 statements: {sorted(missing_findings)}")


def source_closure_digest(document: dict[str, Any], root: Path) -> str:
    semantic_validate_kym(document, root)
    sources = []
    for manifest_ref in document["source_manifest_refs"]:
        manifest_path = resolved_within(root, manifest_ref, "source_manifest_ref")
        manifest = load_json(manifest_path)
        snapshot_path = resolved_within(root, manifest["snapshot_ref"], "source snapshot")
        chunks = []
        for chunk in manifest["chunks"]:
            chunk_path = resolved_within(root, chunk["ref"], "source chunk")
            chunks.append(
                {
                    "id": chunk["id"],
                    "ref": chunk["ref"],
                    "actual_digest": digest_bytes(chunk_path.read_bytes()),
                }
            )
        sources.append(
            {
                "manifest_ref": manifest_ref,
                "manifest_content_digest": reader_manifest_digest(manifest),
                "source_id": manifest["source_id"],
                "snapshot_ref": manifest["snapshot_ref"],
                "snapshot_actual_digest": digest_bytes(snapshot_path.read_bytes()),
                "chunks": chunks,
            }
        )
    return digest_value({"schema": "kym_tco.source_closure.v1", "sources": sources})


def validate_kym_file(
    workspace: Path, product_id: str, path: Path, *, enforce_template: bool = True
) -> dict[str, Any]:
    root = project_root(workspace, product_id)
    document = load_json(path)
    semantic_validate_kym(document, root, enforce_template=enforce_template)
    return document


def _tree_locations(node: dict[str, Any], prefix: str = "") -> dict[str, str]:
    path = f"{prefix}/{node['id']}" if prefix else node["id"]
    result = {node["id"]: path}
    for child in node.get("children", []):
        result.update(_tree_locations(child, path))
    return result


def build_delta(base: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    base_items = {item["id"]: item for item in base["statements"]}
    new_items = {item["id"]: item for item in candidate["statements"]}
    added = sorted(set(new_items) - set(base_items))
    superseded = sorted(
        item_id for item_id, item in new_items.items() if item.get("support_status") == "superseded"
    )
    moved = []
    modified = []
    for item_id in sorted(set(base_items) & set(new_items)):
        before = base_items[item_id]
        after = new_items[item_id]
        if before.get("tree_node_ids") != after.get("tree_node_ids"):
            moved.append(item_id)
        comparable_before = {key: value for key, value in before.items() if key != "tree_node_ids"}
        comparable_after = {key: value for key, value in after.items() if key != "tree_node_ids"}
        if comparable_before != comparable_after:
            modified.append(item_id)
    conflicted = sorted(
        item_id for item_id, item in new_items.items() if item.get("support_status") == "disputed"
    )
    unresolved = sorted(
        item_id for item_id, item in new_items.items() if item.get("support_status") == "unresolved"
    )
    removed = sorted(set(base_items) - set(new_items))
    if removed:
        raise ContractError(f"禁止静默删除 statements；请保留并标记 superseded: {removed}")
    base_targets = {item["id"] for item in base["target_profiles"]}
    candidate_targets = {item["id"] for item in candidate["target_profiles"]}
    removed_targets = sorted(base_targets - candidate_targets)
    if removed_targets:
        raise ContractError(f"禁止在新 revision 中删除既有 Target profiles: {removed_targets}")
    delta = {
        "schema": "kym_tco.kym_delta.v1",
        "product_id": candidate["product_id"],
        "base_revision": base["revision"],
        "candidate_revision": candidate["revision"],
        "base_digest": digest_value(base),
        "candidate_digest": digest_value(candidate),
        "added": added,
        "modified": modified,
        "moved": moved,
        "conflicted": conflicted,
        "superseded": superseded,
        "unresolved": unresolved,
    }
    validate_document("kym_delta", delta)
    return delta


def merge_revision(workspace: Path, product_id: str, base_path: Path, candidate_path: Path) -> tuple[Path, Path]:
    root = project_root(workspace, product_id)
    # 已存的旧 revision 可能早于固定模板合同；它只可作为 immutable base 读取。
    # 所有新 candidate 仍必须通过当前模板校验。
    base = validate_kym_file(workspace, product_id, base_path, enforce_template=False)
    candidate = copy.deepcopy(validate_kym_file(workspace, product_id, candidate_path))
    if candidate["base_revision"] != base["revision"]:
        raise ContractError("candidate.base_revision 与 base revision 不一致")
    revision = next_revision((root / "kym" / "revisions").glob("kym-r*.json"))
    candidate["revision"] = revision
    candidate["created_at"] = utc_now()
    semantic_validate_kym(candidate, root)
    delta = build_delta(base, candidate)
    revision_path = root / "kym" / "revisions" / f"kym-{revision}.json"
    delta_path = root / "kym" / "deltas" / f"{base['revision']}-to-{revision}.json"
    if revision_path.exists():
        raise ContractError(f"revision 已存在，禁止覆盖: {revision_path}")
    write_json(revision_path, candidate)
    write_json(delta_path, delta)
    return revision_path, delta_path


def store_initial_revision(workspace: Path, product_id: str, candidate_path: Path) -> Path:
    root = project_root(workspace, product_id)
    candidate = copy.deepcopy(validate_kym_file(workspace, product_id, candidate_path))
    if list((root / "kym" / "revisions").glob("kym-r*.json")):
        raise ContractError("已有 revision；请使用 kym merge")
    candidate["revision"] = "r001"
    candidate["base_revision"] = None
    candidate["created_at"] = utc_now()
    semantic_validate_kym(candidate, root)
    path = root / "kym" / "revisions" / "kym-r001.json"
    write_json(path, candidate)
    return path


def reader_manifest_digest(manifest: dict[str, Any]) -> str:
    identity = {key: value for key, value in manifest.items() if key != "ingested_at"}
    return digest_value(identity)


def _reader_source_inventory(document: dict[str, Any], root: Path) -> list[dict[str, str]]:
    inventory: list[dict[str, str]] = []
    for manifest_ref in document["source_manifest_refs"]:
        manifest_path = resolved_within(root, manifest_ref, "source_manifest_ref")
        manifest = load_json(manifest_path)
        schema_name = (
            "source_manifest_v2"
            if manifest.get("schema") == "kym_tco.source_manifest.v2"
            else "source_manifest"
        )
        validate_document(schema_name, manifest, manifest_path)
        repo_evidence = manifest.get("repo_evidence")
        if repo_evidence:
            root_ref = repo_evidence["root_ref"].rstrip("/")
            source_files = ", ".join(
                f"{root_ref}/{item['relative_path']}" if root_ref else item["relative_path"]
                for item in repo_evidence["files"]
            )
            repo_revision = repo_evidence.get("git_head") or "unavailable"
            dirty_status = "dirty" if repo_evidence["dirty_status"] else "clean"
            snapshot_kind = repo_evidence["snapshot_kind"]
        else:
            source_files = manifest["original_name"]
            repo_revision = "not_applicable"
            dirty_status = "not_applicable"
            snapshot_kind = "not_applicable"
        inventory.append(
            {
                "source_id": manifest["source_id"],
                "manifest_ref": manifest_ref,
                "source_files": source_files,
                "snapshot_digest": manifest["digest"],
                "manifest_digest": reader_manifest_digest(manifest),
                "repo_revision": repo_revision,
                "dirty_status": dirty_status,
                "snapshot_kind": snapshot_kind,
            }
        )
    return inventory


def render_kym(document: dict[str, Any], root: Path) -> str:
    statements = {item["id"]: item for item in document["statements"]}
    inventory = _reader_source_inventory(document, root)
    lines = [
        f"# KYM：{document['product_id']} ({document['revision']})",
        "",
        "## 来源清单",
        "",
        "| source_id | manifest | source file | snapshot digest | manifest content digest (excludes ingested_at) | repo revision | dirty status | snapshot kind |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for source in inventory:
        lines.append(
            "| "
            + " | ".join(
                f"`{source[field]}`"
                for field in (
                    "source_id",
                    "manifest_ref",
                    "source_files",
                    "snapshot_digest",
                    "manifest_digest",
                    "repo_revision",
                    "dirty_status",
                    "snapshot_kind",
                )
            )
            + " |"
        )
    lines.extend(["", "## KYM 内容", ""])

    def visit(node: dict[str, Any], depth: int) -> None:
        lines.append(f"{'  ' * depth}- {node['title']}")
        for statement_id in node["statement_refs"]:
            item = statements[statement_id]
            lines.append(f"{'  ' * (depth + 1)}- {item['text']}")
        for child in node["children"]:
            visit(child, depth + 1)

    visit(document["tree"], 0)
    if document["target_profiles"]:
        lines.extend(["", "## Target Profiles", ""])
        for target in document["target_profiles"]:
            lines.append(f"### {target['label']} (`{target['id']}`)")
            lines.append("")
            lines.append(target["objective"])
            lines.append("")
            lines.append(f"Claim ceiling：{target['claim_ceiling']}")
            lines.append("")
    rendered = "\n".join(lines).rstrip() + "\n"
    validate_reader_markdown(rendered)
    return rendered


def render_kym_file(workspace: Path, product_id: str, input_path: Path, output_path: Path | None) -> Path:
    root = project_root(workspace, product_id)
    document = validate_kym_file(workspace, product_id, input_path)
    target = output_path or root / "kym" / "views" / f"kym-{document['revision']}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    rendered = render_kym(document, root)
    target.write_text(rendered, encoding="utf-8")
    validate_reader_markdown(target.read_text(encoding="utf-8"), expected=rendered)
    return target


def _approval_receipt(
    product_id: str,
    scope: str,
    artifact_type: str,
    artifact_id: str,
    revision: str,
    artifact: Any,
    approval_reference: str,
    approved_by: str | None,
    source_closure: str | None = None,
) -> dict[str, Any]:
    receipt = {
        "schema": "kym_tco.approval_receipt.v1",
        "receipt_id": f"approve-{scope.replace('_', '-')}-{artifact_id}-{digest_value(artifact).split(':')[1][:12]}",
        "product_id": product_id,
        "approval_scope": scope,
        "artifact_type": artifact_type,
        "artifact_id": artifact_id,
        "revision": revision,
        "artifact_digest": digest_value(artifact),
        "approved_by": approved_by or current_user(),
        "approval_source": "human",
        "approval_reference": approval_reference,
        "approved_at": utc_now(),
    }
    if source_closure is not None:
        receipt["source_closure_digest"] = source_closure
    validate_document("approval", receipt)
    return receipt


def persist_approval_receipt(path: Path, receipt: dict[str, Any]) -> Path:
    if path.exists():
        existing = load_json(path)
        validate_document("approval", existing, path)
        stable_existing = {key: value for key, value in existing.items() if key != "approved_at"}
        stable_candidate = {key: value for key, value in receipt.items() if key != "approved_at"}
        if stable_existing != stable_candidate:
            raise ContractError(f"approval receipt 已存在且批准语义不同，禁止覆盖: {path}")
        return path
    write_json(path, receipt)
    return path


def approve_baseline(
    workspace: Path,
    product_id: str,
    revision_path: Path,
    approval_reference: str,
    approved_by: str | None,
    approval_source: str = "human",
    authorization_path: Path | None = None,
) -> Path:
    root = project_root(workspace, product_id)
    document = validate_kym_file(workspace, product_id, revision_path)
    authorization = None
    if approval_source == "authorized_system":
        authorization = validate_system_authorization(
            root, authorization_path, product_id=product_id, approval_scope="kym_baseline",
            approved_by=approved_by, approval_reference=approval_reference,
            target_ids={item["id"] for item in document["target_profiles"]},
        )
    elif approval_source != "human":
        raise ContractError(f"不支持的 approval_source: {approval_source}")
    elif authorization_path is not None:
        raise ContractError("human approval 不接受系统授权文件")
    receipt = _approval_receipt(
        product_id,
        "kym_baseline",
        "kym_revision",
        document["revision"],
        document["revision"],
        document,
        approval_reference,
        approved_by,
        source_closure_digest(document, root),
    )
    if authorization is not None:
        receipt["approval_source"] = "authorized_system"
        receipt.update(authorization)
        validate_document("approval", receipt)
    path = root / "approvals" / "baselines" / f"{document['revision']}.approval.json"
    return persist_approval_receipt(path, receipt)


def approve_target(
    workspace: Path,
    product_id: str,
    revision_path: Path,
    target_id: str,
    approval_reference: str,
    approved_by: str | None,
    approval_source: str = "human",
    authorization_path: Path | None = None,
) -> Path:
    root = project_root(workspace, product_id)
    document = validate_kym_file(workspace, product_id, revision_path)
    closure_digest = source_closure_digest(document, root)
    baseline_path = root / "approvals" / "baselines" / f"{document['revision']}.approval.json"
    verify_receipt(
        baseline_path,
        document,
        product_id=product_id,
        approval_scope="kym_baseline",
        artifact_type="kym_revision",
        artifact_id=document["revision"],
        revision=document["revision"],
        source_closure_digest=closure_digest,
    )
    targets = {item["id"]: item for item in document["target_profiles"]}
    if target_id not in targets:
        raise ContractError(f"Target 不存在: {target_id}")
    target = targets[target_id]
    statements = {item["id"]: item for item in document["statements"]}
    forbidden_status = {"disputed", "unresolved", "superseded"}
    invalid = [
        statement_id
        for statement_id in target["requirement_refs"]
        if statements[statement_id]["support_status"] in forbidden_status
        or statements[statement_id]["statement_type"] not in {"decision", "requirement", "rule"}
    ]
    if invalid:
        raise ContractError(f"Target 引用了未决/冲突/非 requirement statements: {invalid}")
    authorization = None
    if approval_source == "authorized_system":
        authorization = validate_system_authorization(
            root, authorization_path, product_id=product_id, approval_scope="target_profile",
            approved_by=approved_by, approval_reference=approval_reference, target_ids={target_id},
        )
    elif approval_source != "human":
        raise ContractError(f"不支持的 approval_source: {approval_source}")
    elif authorization_path is not None:
        raise ContractError("human approval 不接受系统授权文件")
    receipt = _approval_receipt(
        product_id,
        "target_profile",
        "target_fragment",
        target_id,
        document["revision"],
        target,
        approval_reference,
        approved_by,
        closure_digest,
    )
    if authorization is not None:
        receipt["approval_source"] = "authorized_system"
        receipt.update(authorization)
        validate_document("approval", receipt)
    path = root / "approvals" / "targets" / f"{document['revision']}-{target_id}.approval.json"
    return persist_approval_receipt(path, receipt)


def verify_receipt(
    path: Path,
    artifact: Any,
    *,
    product_id: str | None = None,
    approval_scope: str | None = None,
    artifact_type: str | None = None,
    artifact_id: str | None = None,
    revision: str | None = None,
    source_closure_digest: str | None = None,
) -> dict[str, Any]:
    receipt = load_json(path)
    validate_document("approval", receipt, path)
    actual = digest_value(artifact)
    if receipt["artifact_digest"] != actual:
        raise ContractError(f"approval receipt digest 已失效: {path}")
    if receipt["approval_scope"] in {"kym_baseline", "target_profile"}:
        if source_closure_digest is None:
            raise ContractError(f"approval receipt consumer 缺少 source closure: {path}")
        if receipt.get("source_closure_digest") != source_closure_digest:
            raise ContractError(f"approval receipt source closure 已失效: {path}")
    expected = {
        "product_id": product_id,
        "approval_scope": approval_scope,
        "artifact_type": artifact_type,
        "artifact_id": artifact_id,
        "revision": revision,
    }
    mismatched = {
        key: {"expected": value, "actual": receipt.get(key)}
        for key, value in expected.items()
        if value is not None and receipt.get(key) != value
    }
    if mismatched:
        raise ContractError(f"approval receipt scope 不匹配: {path}: {mismatched}")
    return receipt
