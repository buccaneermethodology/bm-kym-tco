from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


class ContractError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest_value(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractError(f"文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(f"JSON 无效: {path}: {exc}") from exc


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def require_id(value: str, label: str) -> None:
    if not ID_RE.fullmatch(value):
        raise ContractError(f"{label} 必须是稳定 lowercase id: {value!r}")


def ensure_relative_path(path_text: str, label: str) -> Path:
    path = Path(path_text)
    if path.is_absolute() or ".." in path.parts:
        raise ContractError(f"{label} 必须是 workspace 内相对路径: {path_text}")
    return path


def resolved_within(root: Path, relative: str, label: str) -> Path:
    rel = ensure_relative_path(relative, label)
    resolved_root = root.resolve()
    target = (root / rel).resolve()
    if target != resolved_root and resolved_root not in target.parents:
        raise ContractError(f"{label} 逃逸 workspace: {relative}")
    return target


def next_revision(existing: Iterable[Path]) -> str:
    numbers = []
    for path in existing:
        match = re.fullmatch(r"kym-r(\d{3,})\.json", path.name)
        if match:
            numbers.append(int(match.group(1)))
    return f"r{(max(numbers, default=0) + 1):03d}"


def project_root(workspace: Path, product_id: str) -> Path:
    require_id(product_id, "product_id")
    return workspace.resolve() / ".kym-tco" / product_id


def current_user() -> str:
    return os.environ.get("USER") or "human"

