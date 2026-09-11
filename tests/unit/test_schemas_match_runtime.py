"""schemas/*.json 与运行时的漂移门（审计 B10，2026-09-11）。

背景：三个 schema 文件在 ``src/`` 里**没有任何读者**，也没有门在守它们与运行时的
关系，于是它们各自长成了另一套说法。实测（见 quality/evidence/ 的 B10 探针结论）：

* ``event.schema.json`` 会拒绝 **100% 的真实事件**——它声明
  ``additionalProperties: false`` 却只列 10 个属性（events 表有 14 列），把可空的
  ``contract_id``/``request_id`` 写成必填，``actor`` 枚举还漏掉运行时真正在写的
  ``verifier``/``promoter``/``context``；
* ``executor-manifest.schema.json`` 文档化了运行时没有的 ``user_policy``；
* ``contract.schema.json`` 是唯一与运行时一致的（真实 ``contract/get`` 视图通过校验，
  且 ``tests/unit/test_contract_schema.py`` 一直在正方向校验它）。

本文件守的是**关系**，不是字面清单——所以表加一列、写入点换一个枚举值都会红，
而不是只有手改 schema 才会红：

1. events 表的每一列都必须在 schema 里声明（``additionalProperties: false`` 之下，
   少一列等于拒收真实行）；
2. 真实 ``append_event`` 写出的行必须能通过 schema（写死必填/枚举错就红）；
3. ``src/`` 里每一个字面量 ``append_event(actor=..., role=...)`` 的取值都必须被
   schema 的枚举接受（新写入点引入新值 → 红）；
4. ``executor-manifest`` 的属性集 == ``ExecutorManifest`` 字段与已知设计预留集的并集
   （预留集只许缩短），且真实 manifest 实例必须通过 schema。
"""

from __future__ import annotations

import ast
import dataclasses
import json
import sqlite3
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from lhgp.adapters.manifest import Capabilities, ExecutorManifest, SandboxCapability
from lhgp.contracts.schema import Enforcement
from lhgp.persistence.events_query import append_event
from lhgp.persistence.schema import ensure_schema

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = REPO_ROOT / "schemas"
NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

# 设计文档写了、运行时没有的字段。这是**债务清单**：只许缩短，不许增长。
# 每一项都要能指出它在 DESIGN 的位置——见 executor-manifest.schema.json 里
# user_policy 的 description（DESIGN.md §12.4 的示例里有，ExecutorManifest 没有，
# registry.to_manifest() 也从不产出）。
DESIGN_ONLY_MANIFEST_FIELDS = frozenset({"user_policy"})


def _schema(name: str) -> dict[str, Any]:
    payload = json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _conn(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "state.db")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _jsonable(value: Any) -> Any:
    """把 dataclass / 枚举转成 schema 眼里的 JSON 形状。"""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _errors(instance: object, schema: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(schema)
    return [
        f"{'/'.join(str(p) for p in err.path) or '<根>'}: {err.message}"
        for err in sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
    ]


class TestEventSchemaCoversReality:
    def test_every_stored_event_column_is_declared(self, tmp_path: Path) -> None:
        """events 表有 14 列；schema 声明 additionalProperties: false，少一列即拒收。"""
        conn = _conn(tmp_path)
        try:
            columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(events)")}
        finally:
            conn.close()

        schema = _schema("event")
        declared = set(schema.get("properties", {}))
        missing = sorted(columns - declared)
        assert not missing, (
            f"events 表有而 event.schema.json 未声明的列: {missing}；"
            "在 additionalProperties: false 之下，缺一列就等于拒收所有真实事件"
        )
        assert not sorted(declared - columns), (
            f"event.schema.json 声明了 events 表没有的列: {sorted(declared - columns)}"
        )
        assert set(schema["required"]) <= declared, "required 必须是 properties 的子集"

    def test_required_matches_not_null_columns(self, tmp_path: Path) -> None:
        """必填集必须与 DDL 的 NOT NULL 集一致——可空列被写成必填正是 B10 的成因之一。

        口径：``INTEGER PRIMARY KEY`` 是 rowid 别名，PRAGMA 对它的 ``notnull`` 报 0，
        但它在语义上不可为空，所以主键列也算必填。
        """
        conn = _conn(tmp_path)
        try:
            not_null = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(events)")
                if row["notnull"] or row["pk"]
            }
        finally:
            conn.close()
        schema = _schema("event")
        assert set(schema["required"]) == not_null, (
            "event.schema.json 的 required 与 events 表的 NOT NULL 列不一致："
            f"schema 多={sorted(set(schema['required']) - not_null)}，"
            f"DDL 多={sorted(not_null - set(schema['required']))}"
        )

    def test_real_event_rows_validate(self, tmp_path: Path) -> None:
        """真实 append_event 写出的行必须通过 schema——这是「能不能用」的那条线。"""
        conn = _conn(tmp_path)
        try:
            shapes: list[dict[str, Any]] = [
                # 客户端请求触发的合同事件：有 request_id / revision / goal_id
                {
                    "contract_id": "c-1",
                    "request_id": "req-1",
                    "actor": "user",
                    "role": "user",
                    "contract_revision": 1,
                    "goal_id": "g-1",
                },
                # 守护进程自发事件：没有 request_id，没有 role
                {"contract_id": "c-1", "actor": "daemon"},
                # 核验器写回（B9b 修好的那条通道：role 必须是真的 verifier）
                {
                    "contract_id": "c-1",
                    "request_id": "req-2",
                    "actor": "verifier",
                    "role": "verifier",
                },
                # 全局事件：连合同都没有
                {"contract_id": None, "actor": "context", "role": "promoter"},
            ]
            for shape in shapes:
                append_event(conn, event_type="contract/approved", payload={}, now=NOW, **shape)
            rows = [dict(row) for row in conn.execute("SELECT * FROM events ORDER BY event_id")]
        finally:
            conn.close()

        assert len(rows) == len(shapes)
        schema = _schema("event")
        for row in rows:
            problems = _errors(row, schema)
            assert not problems, f"真实事件行 {row['event_id']} 不通过 schema: {problems}"

    def test_src_writer_literals_fit_schema_enums(self) -> None:
        """src 里每个字面量 append_event(actor=/role=...) 都必须被 schema 的枚举接受。"""
        schema = _schema("event")
        allowed = {
            "actor": set(schema["properties"]["actor"]["enum"]),
            "role": {v for v in schema["properties"]["role"]["enum"] if v is not None},
        }
        offenders: list[str] = []
        checked = 0
        for path in sorted((REPO_ROOT / "src").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name != "append_event":
                    continue
                for kw in node.keywords:
                    if kw.arg not in allowed or not isinstance(kw.value, ast.Constant):
                        continue
                    value = kw.value.value
                    if not isinstance(value, str):
                        continue
                    checked += 1
                    if value not in allowed[kw.arg]:
                        offenders.append(
                            f"{path.relative_to(REPO_ROOT)}:{node.lineno} "
                            f"{kw.arg}={value!r} 不在 schema 枚举 {sorted(allowed[kw.arg])} 内"
                        )
        assert checked, "没有扫到任何字面量写入点——扫描器失效时这条测试会变成假绿"
        assert not offenders, "写入点用了 schema 不接受的取值：\n  " + "\n  ".join(offenders)


class TestManifestSchemaMatchesRuntime:
    def test_property_set_equals_dataclass_fields_plus_design_debt(self) -> None:
        schema = _schema("executor-manifest")
        declared = set(schema.get("properties", {}))
        runtime = {f.name for f in dataclasses.fields(ExecutorManifest)}
        assert declared - runtime == set(DESIGN_ONLY_MANIFEST_FIELDS), (
            "schema 声明了运行时没有的字段（新增的必须进 DESIGN_ONLY_MANIFEST_FIELDS "
            "并说明出处）："
            f"{sorted(declared - runtime - set(DESIGN_ONLY_MANIFEST_FIELDS))}"
        )
        assert not sorted(runtime - declared), (
            f"ExecutorManifest 有而 schema 未声明的字段: {sorted(runtime - declared)}"
        )

    def test_capability_shapes_match(self) -> None:
        schema = _schema("executor-manifest")
        capabilities = schema["properties"]["capabilities"]["properties"]
        assert set(capabilities) == {f.name for f in dataclasses.fields(Capabilities)}
        sandbox = capabilities["sandbox"]["properties"]
        assert set(sandbox) == {f.name for f in dataclasses.fields(SandboxCapability)}

    def test_real_manifest_validates(self) -> None:
        manifest = ExecutorManifest(
            executor_id="fake-executor",
            adapter_version="0.1.0",
            transport="subprocess",
            capabilities=Capabilities(
                spawn=True,
                observe=True,
                cancel=True,
                notify=True,
                followup=False,
                steer=False,
                interrupt=True,
                context="optional",
                sandbox=SandboxCapability(
                    file_effects="workspace-write",
                    network="deny",
                    process="restricted",
                    enforcement=Enforcement.FULL,
                ),
                acceptance_evidence=True,
            ),
            limits={"max_concurrent_attempts": 1, "max_output_bytes": 1_048_576},
        )
        problems = _errors(_jsonable(manifest), _schema("executor-manifest"))
        assert not problems, f"真实 manifest 不通过 schema: {problems}"
