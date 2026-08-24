from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .common import ContractError


SCHEMA_FILES = {
    "config": "project_config_v1.schema.json",
    "source_manifest": "source_manifest_v1.schema.json",
    "source_manifest_v2": "source_manifest_v2.schema.json",
    "kym": "kym_model_v1.schema.json",
    "target_fragment": "target_fragment_v1.schema.json",
    "kym_delta": "kym_delta_v1.schema.json",
    "approval": "approval_receipt_v1.schema.json",
    "authorized_system_authorization": "authorized_system_authorization_v1.schema.json",
    "tco_model": "tco_model_v1.schema.json",
    "coverage": "tco_coverage_model_v1.schema.json",
    "tco_model_v2": "tco_model_v2.schema.json",
    "tco_model_v3": "tco_model_v3.schema.json",
    "tco_model_v4": "tco_model_v4.schema.json",
    "coverage_v2": "tco_coverage_model_v2.schema.json",
    "coverage_v3": "tco_coverage_model_v3.schema.json",
    "coverage_report_v2": "tco_coverage_report_v2.schema.json",
    "coverage_report_v3": "tco_coverage_report_v3.schema.json",
    "tco_execution_result": "tco_execution_result_v1.schema.json",
    "executable_package_contract": "executable_package_contract_v1.schema.json",
    "executable_package_cases": "executable_package_cases_v1.schema.json",
    "doctor_report": "doctor_report_v1.schema.json",
    "kym_package_manifest": "kym_package_manifest_v1.schema.json",
    "kym_package_artifact_index": "kym_package_artifact_index_v1.schema.json",
    "kym_package_lineage": "kym_package_lineage_v1.schema.json",
    "tco_package_manifest": "tco_package_manifest_v1.schema.json",
    "tco_package_artifact_index": "tco_package_artifact_index_v1.schema.json",
    "evidence_hub_integrity": "evidence_hub_integrity_v1.schema.json",
    "tco_multi_input_package_manifest": "tco_multi_input_package_manifest_v1.schema.json",
}


def schema_document(kind: str) -> dict[str, Any]:
    if kind not in SCHEMA_FILES:
        raise ContractError(f"未知 schema kind: {kind}")
    text = resources.files("kym_tco.schemas").joinpath(SCHEMA_FILES[kind]).read_text(encoding="utf-8")
    return json.loads(text)


def validate_document(kind: str, document: Any, source: Path | None = None) -> None:
    validator = Draft202012Validator(schema_document(kind))
    errors = sorted(validator.iter_errors(document), key=lambda item: list(item.path))
    if errors:
        prefix = f"{source}: " if source else ""
        details = []
        for error in errors:
            location = ".".join(str(part) for part in error.path) or "<root>"
            details.append(f"{location}: {error.message}")
        raise ContractError(prefix + "schema 校验失败:\n- " + "\n- ".join(details))
