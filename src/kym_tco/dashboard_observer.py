"""Read-only Dashboard observation for Slice 5.

The observer never edits Dashboard/Sessions.md.  Its output is an immutable
observation that a separate governance decision may authorize a remediator to
consume.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .common import ContractError, digest_bytes, digest_value

ALLOWED_STATUSES = {"todo", "doing", "blocked", "decision-needed", "done", "archived"}
SESSION_RE = re.compile(r"^\|\s*(S-\d+)\s*\|")


def _row_fields(line: str) -> list[str]:
    return [cell.strip() for cell in line.split("|")[1:-1]]


def _route(fields: list[str], status: str) -> str:
    text = " ".join(fields).casefold()
    if status == "archived":
        return "superseded_state_debt"
    if status == "done":
        return "release_governance_or_one_time"
    if "m1" in text or "m0" in text:
        return "m1_or_gap_m1"
    if any(token in text for token in ("release", "governance", "skill", "版本", "一次性", "ui", "renderer")):
        return "release_governance_or_one_time"
    if status in {"todo", "doing", "blocked", "decision-needed"}:
        return "gap_or_follow_on"
    return "unresolved"


def observe_dashboard(workspace: Path, dashboard_ref: str = "Dashboard/Sessions.md") -> dict[str, Any]:
    path = (workspace / dashboard_ref).resolve()
    if not path.is_file():
        raise ContractError(f"Dashboard Sessions 不存在: {dashboard_ref}")
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    sessions: list[dict[str, Any]] = []
    occurrences: dict[str, list[dict[str, Any]]] = {}
    header: list[str] = []
    for line_no, line in enumerate(text.splitlines(keepends=True), 1):
        if line.startswith("|") and "Status" in line and not header:
            header = _row_fields(line)
            continue
        match = SESSION_RE.match(line)
        if not match:
            continue
        fields = _row_fields(line)
        if not fields:
            continue
        session_id = match.group(1)
        status_index = next((index for index, name in enumerate(header) if name.casefold() == "status"), 5)
        status = fields[status_index] if status_index < len(fields) else ""
        occurrence = {
            "occurrence_id": f"{session_id}#occurrence-{len(occurrences.get(session_id, [])) + 1}",
            "session_id": session_id, "line_no": line_no,
            "row_byte_digest": digest_bytes(line.encode("utf-8")), "row_text": line,
            "fields": fields, "status": status, "routing_class": _route(fields, status),
        }
        occurrences.setdefault(session_id, []).append(occurrence)
        sessions.append(occurrence)
    duplicate_findings = [
        {"session_id": session_id, "occurrence_ids": [item["occurrence_id"] for item in items], "reason": "duplicate_session_id"}
        for session_id, items in sorted(occurrences.items()) if len(items) > 1
    ]
    invalid_statuses = [item["occurrence_id"] for item in sessions if item["status"] not in ALLOWED_STATUSES]
    unresolved_sessions = [item["occurrence_id"] for item in sessions if item["routing_class"] == "unresolved"]
    return {
        "schema": "kym_tco.dashboard_duplicate_observation.v1",
        "dashboard_ref": dashboard_ref, "pre_fix_byte_digest": digest_bytes(raw),
        "header": header, "session_count": len(sessions), "sessions": sessions,
        "duplicate_findings": duplicate_findings, "invalid_status_occurrences": invalid_statuses,
        "unresolved_occurrences": unresolved_sessions,
        "claim_ceiling": "dashboard_completeness_observation_only_no_coverage_or_runtime_authority",
        "observation_digest": digest_value({
            "schema": "kym_tco.dashboard_duplicate_observation.v1", "dashboard_ref": dashboard_ref,
            "pre_fix_byte_digest": digest_bytes(raw), "header": header, "session_count": len(sessions),
            "sessions": sessions, "duplicate_findings": duplicate_findings,
            "invalid_status_occurrences": invalid_statuses, "unresolved_occurrences": unresolved_sessions,
        }),
    }


def validate_observation(workspace: Path, observation: dict[str, Any]) -> dict[str, Any]:
    if observation.get("schema") != "kym_tco.dashboard_duplicate_observation.v1":
        raise ContractError("Dashboard observation schema mismatch")
    current = observe_dashboard(workspace, observation["dashboard_ref"])
    if current["pre_fix_byte_digest"] != observation["pre_fix_byte_digest"]:
        raise ContractError("Dashboard pre-fix bytes changed after observation")
    return observation
