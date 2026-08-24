#!/usr/bin/env python3
"""Install repo-local Codex skills safely; default operation is dry-run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPO_ROOT / ".codex" / "skills"
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class InstallError(ValueError):
    pass


def _digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _inventory(root: Path) -> dict[str, str]:
    if root.is_symlink():
        raise InstallError(f"symlink is not allowed: {root}")
    inventory: dict[str, str] = {}
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in sorted(directories):
            candidate = current_path / name
            if candidate.is_symlink():
                raise InstallError(f"symlink is not allowed: {candidate}")
        for name in sorted(files):
            candidate = current_path / name
            if candidate.is_symlink():
                raise InstallError(f"symlink is not allowed: {candidate}")
            relative = candidate.relative_to(root).as_posix()
            if relative.startswith("../") or Path(relative).is_absolute():
                raise InstallError(f"path escapes skill root: {relative}")
            inventory[relative] = _digest(candidate)
    return dict(sorted(inventory.items()))


def _skill_sources(source: Path, selected_skills: list[str] | None = None) -> list[Path]:
    if source.is_symlink() or not source.is_dir():
        raise InstallError(f"source must be a non-symlink directory: {source}")
    selected_skills = selected_skills or None
    if selected_skills:
        if len(selected_skills) != len(set(selected_skills)):
            raise InstallError("each --skill value must be specified at most once")
        invalid_names = [name for name in selected_skills if not SKILL_NAME_RE.fullmatch(name)]
        if invalid_names:
            raise InstallError(f"invalid skill name: {invalid_names[0]}")
        children = [source / name for name in selected_skills]
    else:
        children = sorted(source.iterdir(), key=lambda item: item.name)

    result: list[Path] = []
    for child in children:
        if child.is_symlink():
            raise InstallError(f"symlink is not allowed: {child}")
        if selected_skills and (not child.exists() or not child.is_dir()):
            raise InstallError(f"requested skill was not found: {child.name}")
        if not child.is_dir():
            continue
        if not SKILL_NAME_RE.fullmatch(child.name):
            raise InstallError(f"invalid skill directory name: {child.name}")
        if not (child / "SKILL.md").is_file() or not (child / "agents/openai.yaml").is_file():
            raise InstallError(f"skill is missing SKILL.md or agents/openai.yaml: {child.name}")
        _inventory(child)
        result.append(child)
    if not result:
        raise InstallError(f"no skills found under {source}")
    return result


def _safe_target(source: Path, target: Path) -> Path:
    source_resolved = source.resolve()
    target_absolute = target.absolute()
    if target_absolute.is_symlink():
        raise InstallError(f"target root must not be a symlink: {target}")
    target_resolved = target_absolute.resolve(strict=False)
    if target_resolved == source_resolved:
        raise InstallError("target must differ from the repo-local source")
    try:
        target_resolved.relative_to(source_resolved)
    except ValueError:
        pass
    else:
        raise InstallError("target must not be inside the repo-local source")
    if target_resolved.exists() and not target_resolved.is_dir():
        raise InstallError(f"target must be a directory: {target}")
    return target_resolved


def plan(
    source: Path, target: Path, selected_skills: list[str] | None = None
) -> list[dict[str, Any]]:
    target = _safe_target(source, target)
    actions: list[dict[str, Any]] = []
    for skill in _skill_sources(source, selected_skills):
        destination = target / skill.name
        try:
            destination.relative_to(target)
        except ValueError as exc:
            raise InstallError(f"destination escapes target: {destination}") from exc
        if destination.is_symlink():
            raise InstallError(f"target skill must not be a symlink: {destination}")
        if not destination.exists():
            actions.append({"skill": skill.name, "action": "create"})
            continue
        if not destination.is_dir():
            raise InstallError(f"target skill is not a directory: {destination}")
        source_inventory = _inventory(skill)
        target_inventory = _inventory(destination)
        if source_inventory != target_inventory:
            raise InstallError(f"refusing non-identical overwrite: {skill.name}")
        actions.append({"skill": skill.name, "action": "unchanged"})
    return actions


def install(source: Path, target: Path, actions: list[dict[str, Any]]) -> None:
    target = _safe_target(source, target)
    target.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise InstallError(f"target root must not be a symlink: {target}")
    for action in actions:
        if action["action"] != "create":
            continue
        source_skill = source / action["skill"]
        destination = target / action["skill"]
        shutil.copytree(source_skill, destination, symlinks=False)
        if _inventory(source_skill) != _inventory(destination):
            raise InstallError(f"post-copy verification failed: {action['skill']}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    result.add_argument("--target", type=Path, required=True)
    result.add_argument(
        "--skill",
        action="append",
        default=[],
        metavar="NAME",
        help="install only this Skill; repeat to select multiple Skills",
    )
    mode = result.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="report actions without writing; default")
    mode.add_argument("--install", action="store_true", help="explicitly install missing skills")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        source = args.source.resolve()
        actions = plan(source, args.target, args.skill or None)
        operation = "install" if args.install else "dry-run"
        if args.install:
            install(source, args.target, actions)
        report = {
            "schema_version": "codex_skill_install_report_v1",
            "operation": operation,
            "wrote": bool(args.install and any(item["action"] == "create" for item in actions)),
            "actions": actions,
        }
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except (InstallError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
