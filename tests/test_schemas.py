"""结果格式规范的自洽性：schema 与任务集标签必须同步，避免报告口径漂移。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from retail_support_agent.tau2_pin import REPO_ROOT, load_task_set

SCHEMA_DIR = REPO_ROOT / "evaluation" / "schemas"


def _load(name: str) -> dict[str, Any]:
    path = SCHEMA_DIR / name
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


@pytest.mark.parametrize("name", ["run_manifest.schema.json", "task_result.schema.json"])
def test_schema_is_valid_json_with_identity(name: str) -> None:
    schema = _load(name)
    assert schema["$schema"].startswith("https://json-schema.org/")
    assert schema["$id"]
    assert schema["type"] == "object"
    assert schema["required"]


def test_scope_label_enum_matches_task_set_definitions() -> None:
    enum = set(_load("task_result.schema.json")["properties"]["scope_label"]["enum"])
    defined = set(load_task_set("scope_labels")["definitions"])
    assert enum == defined, "结果 schema 的范围标签与任务集定义必须一致"


def test_run_manifest_requires_frozen_evaluation_stack() -> None:
    schema = _load("run_manifest.schema.json")
    required = set(schema["required"])
    # 模拟用户与评判模型若不随运行记录冻结，跨模型对比结论无法成立
    assert {"user_simulator", "judge", "upstream", "code"} <= required
    assert "verified" in schema["properties"]["upstream"]["required"]


def test_task_result_separates_failure_kinds() -> None:
    outcomes = set(_load("task_result.schema.json")["properties"]["outcome"]["enum"])
    # 业务失败与服务错误、参数不支持必须可区分，否则失败率会被稀释
    assert {"task_failure", "service_error", "unsupported_param"} <= outcomes


def test_schema_dir_contains_only_declared_schemas() -> None:
    found = {p.name for p in Path(SCHEMA_DIR).glob("*.json")}
    assert found == {"run_manifest.schema.json", "task_result.schema.json"}
