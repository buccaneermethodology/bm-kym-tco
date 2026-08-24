from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable


PROTECTED = {"approval", "approvals", "receipt", "receipts", "authorization", "authorizations", ".git"}


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _allowed(path: Path, allowed_root: Path) -> bool:
    try:
        relative = path.resolve().relative_to(allowed_root.resolve())
    except ValueError:
        return False
    return not bool({part.lower() for part in relative.parts} & PROTECTED)


def _href(path: Path, html_path: Path) -> str:
    return Path(os.path.relpath(path.resolve(), html_path.parent.resolve())).as_posix()


def build_integrity_report(
    *,
    html_path: Path,
    allowed_root: Path,
    artifacts: Iterable[tuple[str, Path]],
    claim_ceiling: str,
    state_axes: dict[str, str],
    expected_digests: dict[str, str] | None = None,
    finding_owner: str = "none",
    path_root: Path | None = None,
) -> dict[str, Any]:
    rows = []
    blocked = False
    for label, path in artifacts:
        resolved = path.resolve()
        exists = resolved.is_file()
        allowed = _allowed(resolved, allowed_root) if exists else False
        actual_digest = _digest(resolved) if exists and allowed else None
        expected = (expected_digests or {}).get(label)
        stale = expected is not None and actual_digest != expected
        status = "passed" if exists and allowed and not stale else "blocked"
        blocked = blocked or status != "passed"
        rows.append(
            {
                "label": label,
                "href": _href(resolved, html_path),
                "path": resolved.relative_to((path_root or allowed_root).resolve()).as_posix() if exists and allowed and (path_root or allowed_root).resolve() in resolved.parents else None,
                "status": status,
                "digest": actual_digest,
                "expected_digest": expected,
                "reason": None if status == "passed" else "stale_digest" if stale else "missing_or_protected_or_outside_allowed_root",
            }
        )
    return {
        "schema": "kym_tco.evidence_hub_integrity.v1",
        "status": "blocked" if blocked else "passed",
        "claim_ceiling": claim_ceiling,
        "state_axes": state_axes,
        "finding_owner": finding_owner,
        "html_href": html_path.name,
        "artifacts": rows,
        "authority_note": "HTML 只展示 candidate evidence；integrity failure 不创建 approval、receipt 或 semantic verdict。",
    }


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def render_failure_html(manifest: dict[str, Any], integrity: dict[str, Any]) -> str:
    def esc(value: Any) -> str:
        import html
        return html.escape(str(value))

    commands = "".join(
        f"<tr><td>{esc(item.get('step'))}</td><td>{esc(' '.join(item.get('argv', [])))}</td><td>{esc(item.get('exit_code'))}</td><td>{esc(item.get('summary'))}</td><td>{esc(item.get('evidence_path'))}</td></tr>"
        for item in manifest.get("command_evidence", [])
    )
    links = "".join(
        f"<tr><td>{esc(item['label'])}</td><td><a href=\"{esc(item['href'])}\">{esc(item['href'])}</a></td><td>{esc(item['status'])}</td><td>{esc(item.get('digest') or item.get('reason') or '')}</td></tr>"
        for item in integrity.get("artifacts", [])
    )
    axes = "".join(f"<dt>{esc(key)}</dt><dd>{esc(value)}</dd>" for key, value in manifest.get("state_axes", {}).items())
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>{esc(manifest.get("product_id"))} Failure Evidence Hub</title>
<style>body{{font-family:Arial,"PingFang SC",sans-serif;max-width:1100px;margin:32px auto;padding:0 18px;color:#172033}}section{{border:1px solid #cbd5e1;border-radius:8px;padding:16px;margin:14px 0}}table{{border-collapse:collapse;width:100%}}th,td{{text-align:left;vertical-align:top;border-bottom:1px solid #e2e8f0;padding:8px}}.blocked{{color:#991b1b;font-weight:700}}dt{{font-weight:700}}dd{{margin:0 0 6px}}</style>
<h1>失败证据中心 / Failure Evidence Hub</h1><p class="blocked">{esc(manifest.get("result"))} · {esc(manifest.get("finding_code"))}</p>
<section><h2>状态与责任 owner</h2><dl>{axes}</dl><p><strong>Finding owner：</strong>{esc(integrity.get("finding_owner"))}</p><p><strong>Claim ceiling：</strong>{esc(integrity.get("claim_ceiling"))}</p></section>
<section><h2>失败命令</h2><table><thead><tr><th>Step</th><th>argv</th><th>exit</th><th>摘要</th><th>证据</th></tr></thead><tbody>{commands}</tbody></table></section>
<section><h2>Artifact links / digest integrity</h2><p>Integrity：{esc(integrity.get("status"))}</p><table><thead><tr><th>Artifact</th><th>Link</th><th>Status</th><th>Digest/reason</th></tr></thead><tbody>{links}</tbody></table></section>
<p>{esc(integrity.get("authority_note"))}</p></html>'''
