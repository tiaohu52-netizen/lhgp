"""请求信封解析（DESIGN §11.1）。

fail-closed：字段缺失、版本不符、空标识一律 VALIDATION_FAILED。
"""

from __future__ import annotations

import pytest

from longtask import PROTOCOL_VERSION
from longtask.rpc.errors import ErrorCode, RpcError
from longtask.rpc.methods import Method
from longtask.rpc.server import parse_envelope, route

pytestmark = pytest.mark.unit


def make_raw(**overrides: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "method": "contract/get",
        "request_id": "req-001",
        "client_id": "cli-local",
        "protocol_version": PROTOCOL_VERSION,
        "params": {"contract_id": "lt-20260831-001"},
    }
    raw.update(overrides)
    return raw


class TestParseEnvelope:
    def test_valid_envelope(self) -> None:
        env = parse_envelope(make_raw())
        assert env.method is Method.CONTRACT_GET
        assert env.request_id == "req-001"
        assert env.params["contract_id"] == "lt-20260831-001"

    def test_missing_field_rejected(self) -> None:
        raw = make_raw()
        del raw["request_id"]
        with pytest.raises(RpcError, match="malformed") as exc_info:
            parse_envelope(raw)
        assert exc_info.value.code is ErrorCode.VALIDATION_FAILED

    def test_unknown_method_rejected(self) -> None:
        with pytest.raises(RpcError):
            parse_envelope(make_raw(method="contract/teleport"))

    def test_version_mismatch_rejected(self) -> None:
        with pytest.raises(RpcError, match="protocol_version 99 unsupported"):
            parse_envelope(make_raw(protocol_version=99))

    def test_boolean_version_rejected(self) -> None:
        with pytest.raises(RpcError, match="protocol_version must be an integer"):
            parse_envelope(make_raw(protocol_version=True))

    def test_empty_identifiers_rejected(self) -> None:
        with pytest.raises(RpcError, match="non-empty"):
            parse_envelope(make_raw(request_id=""))

    @pytest.mark.parametrize("field", ["request_id", "client_id"])
    def test_non_string_identifiers_rejected(self, field: str) -> None:
        with pytest.raises(RpcError, match="must be strings"):
            parse_envelope(make_raw(**{field: 7}))

    @pytest.mark.parametrize("params", [None, [], "not-an-object"])
    def test_non_object_params_rejected(self, params: object) -> None:
        with pytest.raises(RpcError, match="params must be an object"):
            parse_envelope(make_raw(params=params))


class TestRoute:
    def test_unimplemented_method_reports_state_forbidden(self) -> None:
        # attempt/status 在 Developer Preview 已实现：用一个未实现的方法验证
        env = parse_envelope(make_raw(method="control/spawn"))
        with pytest.raises(RpcError, match="not implemented") as exc_info:
            route(env)
        assert exc_info.value.code is ErrorCode.STATE_FORBIDDEN
        assert exc_info.value.retryable is False

    def test_hello_route_dispatches(self) -> None:
        env = parse_envelope(make_raw(method="protocol/hello"))
        res = route(env)
        assert res["ok"] is True
        assert res["result"]["protocol_version"] == PROTOCOL_VERSION
        assert "contract/prepare" in res["result"]["methods"]
