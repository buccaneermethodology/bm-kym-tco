from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import ContractError, digest_value, load_json, project_root, write_json
from .validation import validate_document


DEFAULT_STATES = [
    {"id": "unknown", "rank": 0, "label": "未知", "definition": "尚未完成 evidence review。", "reserved": True},
    {"id": "not_started", "rank": 10, "label": "未开始", "definition": "已确认没有对应 evidence。", "reserved": True},
    {"id": "design_only", "rank": 20, "label": "设计/合同", "definition": "只有设计或合同证据。", "reserved": True},
    {"id": "schema_validator", "rank": 30, "label": "Schema/Validator", "definition": "已有机器可验证结构或 validator。", "reserved": True},
    {"id": "test_bound", "rank": 40, "label": "测试边界", "definition": "已有受维护测试边界。", "reserved": True},
    {"id": "bounded_runtime", "rank": 50, "label": "有界 Runtime", "definition": "已有明确 claim ceiling 的 runtime behavior evidence。", "reserved": True},
    {"id": "integrated_runtime", "rank": 60, "label": "集成 Runtime", "definition": "已有受维护的跨组件 runtime integration evidence。", "reserved": True},
]
DEFAULT_DECISION_OUTCOME_VOCABULARY = {
    "id": "system-default-v1",
    "outcomes": ["accepted", "rejected", "approved", "denied", "eligible", "ineligible", "pending_review", "blocked", "failed", "diagnostic", "not_applicable"],
    "forbidden_aliases": {"pending review": "pending_review", "review queued": "pending_review"},
}


def default_config(product_id: str, coverage_mode: str) -> dict[str, Any]:
    vocabulary = {"states": DEFAULT_STATES}
    return {
        "schema": "kym_tco.project_config.v1",
        "product_id": product_id,
        "coverage_mode": coverage_mode,
        "source_chunk_max_chars": 20000,
        "coverage_vocabulary": vocabulary,
        "coverage_vocabulary_digest": digest_value(vocabulary),
        "decision_outcome_vocabulary": DEFAULT_DECISION_OUTCOME_VOCABULARY,
        "decision_outcome_vocabulary_digest": digest_value(DEFAULT_DECISION_OUTCOME_VOCABULARY),
        "evidence_roots": [],
        "verification_commands": [],
    }


def init_project(workspace: Path, product_id: str, coverage_mode: str = "repo") -> Path:
    if coverage_mode not in {"repo", "not_evaluated"}:
        raise ContractError("coverage_mode 必须是 repo 或 not_evaluated")
    root = project_root(workspace, product_id)
    root.mkdir(parents=True, exist_ok=True)
    for relative in (
        "sources/snapshots",
        "sources/chunks",
        "kym/drafts",
        "kym/revisions",
        "kym/views",
        "kym/deltas",
        "approvals/baselines",
        "approvals/targets",
        "tco/models",
        "tco/coverage",
        "tco/proposals",
        "tco/approvals",
        "reports",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    config_path = root / "config.json"
    if not config_path.exists():
        write_json(config_path, default_config(product_id, coverage_mode))
    return root


def load_config(root: Path) -> dict[str, Any]:
    config_path = root / "config.json"
    config = load_json(config_path)
    validate_document("config", config, config_path)
    decision_vocabulary = config.get("decision_outcome_vocabulary", DEFAULT_DECISION_OUTCOME_VOCABULARY)
    decision_digest = digest_value(decision_vocabulary)
    if config.get("decision_outcome_vocabulary_digest", decision_digest) != decision_digest:
        raise ContractError("decision outcome vocabulary digest 与配置内容不一致")
    if len(decision_vocabulary["outcomes"]) != len(set(decision_vocabulary["outcomes"])):
        raise ContractError("decision outcome vocabulary 的 outcomes 必须唯一")
    if not set(decision_vocabulary["forbidden_aliases"].values()) <= set(decision_vocabulary["outcomes"]):
        raise ContractError("decision outcome vocabulary alias 必须映射到既有 outcome")
    config["decision_outcome_vocabulary"] = decision_vocabulary
    config["decision_outcome_vocabulary_digest"] = decision_digest
    vocabulary = {"states": config["coverage_vocabulary"]["states"]}
    actual = digest_value(vocabulary)
    if config["coverage_vocabulary_digest"] != actual:
        raise ContractError("coverage vocabulary digest 与配置内容不一致")
    states = config["coverage_vocabulary"]["states"]
    ids = [item["id"] for item in states]
    ranks = [item["rank"] for item in states]
    if len(ids) != len(set(ids)) or len(ranks) != len(set(ranks)):
        raise ContractError("coverage vocabulary 的 id/rank 必须唯一")
    command_ids = [item["id"] for item in config["verification_commands"]]
    if len(command_ids) != len(set(command_ids)):
        raise ContractError("verification command id 必须唯一")
    required = {item["id"]: item for item in DEFAULT_STATES}
    actual_by_id = {item["id"]: item for item in states}
    for state_id, expected in required.items():
        if state_id not in actual_by_id:
            raise ContractError(f"缺少保留 coverage state: {state_id}")
        for key in ("rank", "definition"):
            if actual_by_id[state_id][key] != expected[key]:
                raise ContractError(f"保留 coverage state 不得改变 {key}: {state_id}")
    return config


def state_rank(config: dict[str, Any], state_id: str) -> int:
    for state in config["coverage_vocabulary"]["states"]:
        if state["id"] == state_id:
            return int(state["rank"])
    raise ContractError(f"coverage state 未在 vocabulary 中声明: {state_id}")
