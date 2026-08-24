from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from . import __version__
from .common import ContractError, project_root, require_id, write_json
from .validation import validate_document
from .workspace import load_config


TOOL_SKILLS = {"kym": "build-kym", "tco": "build-tco-coverage"}
STATUS_PRIORITY = [
    "cli_missing", "skill_missing", "version_mismatch",
    "workspace_invalid", "permission_blocked", "ready",
]
FINDINGS = {
    "cli_missing": ("ENV_CLI_MISSING", "CLI 不可用；Skill 不能执行本机确定性检查。"),
    "skill_missing": ("ENV_SKILL_MISSING", "对应 Skill 缺失或 identity 不匹配。"),
    "version_mismatch": ("ENV_VERSION_MISMATCH", "Skill 期望的 CLI 版本与当前版本不一致。"),
    "workspace_invalid": ("ENV_WORKSPACE_INVALID", "product workspace 未初始化、product-id 无效或 config 校验失败。"),
    "permission_blocked": ("ENV_PERMISSION_BLOCKED", "workspace 或显式报告输出路径不可写，或命中 write exclusion。"),
}
REMEDIATION = {
    "cli_missing": "安装与 Skill 版本匹配的 kym-tco CLI，然后重新运行 kym --help、tco --help 和 doctor。",
    "skill_missing": "安装或选择对应版本的 build-kym / build-tco-coverage Skill，并传入其目录。",
    "version_mismatch": "使用同一固定 release tag 的 Skill 与 CLI，或显式选择兼容版本后重试。",
    "workspace_invalid": "先在目标 workspace 运行 kym init 或 tco init，并检查 product-id 与 config.json。",
    "permission_blocked": "选择可写 workspace/报告路径；不要把 doctor 输出写入 approval、receipt、authorization、KB、Dashboard 或 .git 路径。",
}
CLAIM_CEILING = (
    "本报告只说明当前输入下的环境诊断状态；ready 不证明语义正确、candidate 已生成、"
    "verification/audit 已运行、approval 已请求或生产就绪。"
)


def _skill_is_valid(skill_dir: Path, expected_name: str) -> bool:
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.is_file():
        return False
    try:
        header = "\n".join(skill_file.read_text(encoding="utf-8").splitlines()[:12])
    except OSError:
        return False
    return f"name: {expected_name}" in header


def _nearest_existing(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _mode_writable(path: Path) -> bool:
    target = _nearest_existing(path)
    try:
        mode = target.stat().st_mode
    except OSError:
        return False
    write_bits = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    return bool(mode & write_bits) and os.access(target, os.W_OK)


def _output_is_excluded(path: Path) -> bool:
    lowered = path.as_posix().lower()
    parts = {part.lower() for part in path.parts}
    return (
        "approvals" in parts or "receipt" in lowered or "authorization" in lowered
        or ".git" in parts or "/kb/data/" in lowered or "dashboard" in parts
    )


def _workspace_is_valid(workspace: Path, product_id: str) -> tuple[bool, Path | None]:
    try:
        require_id(product_id, "product_id")
        root = project_root(workspace, product_id)
        if not (root / "config.json").is_file():
            return False, root
        load_config(root)
        return True, root
    except (ContractError, OSError, ValueError):
        return False, None


def run_doctor(
    *,
    tool: str,
    workspace: Path,
    product_id: str,
    skill_dir: Path,
    expected_cli_version: str,
    output: Path | None = None,
    cli_available: bool = True,
) -> dict[str, Any]:
    if tool not in TOOL_SKILLS:
        raise ContractError(f"未知 doctor tool: {tool}")
    workspace = workspace.resolve()
    skill_dir = skill_dir.resolve()
    output = output.resolve() if output is not None else None
    expected_skill = TOOL_SKILLS[tool]
    workspace_valid, root = _workspace_is_valid(workspace, product_id)

    failed = {
        "cli_missing": not cli_available,
        "skill_missing": not _skill_is_valid(skill_dir, expected_skill),
        "version_mismatch": cli_available and __version__ != expected_cli_version,
        "workspace_invalid": not workspace_valid,
        "permission_blocked": False,
    }
    writable_target = output.parent if output is not None else (root or workspace)
    if not _mode_writable(writable_target) or (output is not None and _output_is_excluded(output)):
        failed["permission_blocked"] = True

    def build_report(posture: str) -> dict[str, Any]:
        status = next((name for name in STATUS_PRIORITY if failed.get(name)), "ready")
        findings = [
            {"code": FINDINGS[name][0], "status": name, "message": FINDINGS[name][1]}
            for name in STATUS_PRIORITY[:-1]
            if failed[name]
        ]
        report = {
            "schema": "kym_tco.doctor_report.v1",
            "tool": tool,
            "skill_name": expected_skill,
            "cli_version": __version__ if cli_available else None,
            "expected_cli_version": expected_cli_version,
            "workspace": str(workspace),
            "product_id": product_id,
            "environment_status": status,
            "package_execution_status": "not_started",
            "candidate_status": "not_generated",
            "verification_status": "not_run",
            "audit_status": "not_run",
            "approval_status": "not_requested",
            "findings": findings,
            "remediation": [REMEDIATION[item["status"]] for item in findings],
            "checked_paths": [
                str(skill_dir / "SKILL.md"),
                str((root or workspace / ".kym-tco" / product_id) / "config.json"),
                *( [str(output)] if output is not None else [] ),
            ],
            "output_posture": posture,
            "output_path": str(output) if output is not None else None,
            "claim_ceiling": CLAIM_CEILING,
        }
        validate_document("doctor_report", report)
        return report

    if output is None:
        return build_report("stdout_only")
    if failed["permission_blocked"]:
        return build_report("stdout_fallback")

    report = build_report("file_and_stdout")
    try:
        write_json(output, report)
    except OSError:
        failed["permission_blocked"] = True
        return build_report("stdout_fallback")
    return report
