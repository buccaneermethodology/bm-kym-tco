from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .common import ContractError, digest_value, load_json
from .validation import validate_document


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"系统授权 expires_at 无效: {value}") from exc
    if parsed.tzinfo is None:
        raise ContractError("系统授权 expires_at 必须带时区")
    return parsed.astimezone(timezone.utc)


def validate_system_authorization(
    project_root: Path,
    authorization_path: Path | None,
    *,
    product_id: str,
    approval_scope: str,
    approved_by: str | None,
    approval_reference: str,
    target_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Validate an explicit, workspace-local authorization for a system receipt.

    This is an operational authorization binding, not a replacement for the
    authorization issuer's own access control.  The CLI never creates it.
    """
    if not approved_by:
        raise ContractError("authorized_system 必须显式提供 approved_by 系统主体")
    if authorization_path is None:
        raise ContractError("authorized_system 必须提供系统授权文件")
    authorized_root = (project_root / "automation" / "authorizations").resolve()
    path = authorization_path.resolve()
    if path.parent != authorized_root:
        raise ContractError("系统授权文件必须位于 .kym-tco/<product>/automation/authorizations/")
    if not path.is_file():
        raise ContractError(f"系统授权文件不存在: {path}")
    authorization = load_json(path)
    validate_document("authorized_system_authorization", authorization, path)
    if authorization["product_id"] != product_id:
        raise ContractError("系统授权 product_id 不一致")
    if authorization["system_id"] != approved_by:
        raise ContractError("系统授权 system_id 与 approved_by 不一致")
    if approval_scope not in authorization["approval_scopes"]:
        raise ContractError(f"系统授权不允许当前 approval_scope: {approval_scope}")
    if not approval_reference.startswith(authorization["approval_reference_prefix"]):
        raise ContractError("approval_reference 不属于系统授权前缀")
    if _parse_utc(authorization["expires_at"]) < datetime.now(timezone.utc):
        raise ContractError("系统授权已过期")
    allowed_targets = set(authorization["target_ids"])
    if target_ids and not target_ids <= allowed_targets:
        raise ContractError(
            f"系统授权不允许当前 Target: {sorted(target_ids - allowed_targets)}"
        )
    return {
        "authorization_id": authorization["authorization_id"],
        "authorization_digest": digest_value(authorization),
    }
