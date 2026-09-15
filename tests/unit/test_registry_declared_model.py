"""declared_model / effective_model：执行器「声明模型 = 记录模型」一致性。

背景（2026-09-15）：派工记录此前取 ``models`` 列表首个非通配项，而 CLI
实际执行的模型写在 ``launch.argv``（``--model``）里——两者可以不一致且无告警，
审计看到的是配置副本而非事实。本组测试钉住修复后的语义：

- ``from_dict`` 从 argv 解析 ``declared_model``（``--model X`` / ``--model=X``）；
- 校验失败即拒绝条目（白名单外 / 具体化 models 却未声明）；
- ``effective_model()`` 取声明值优先，供 dispatch / verifier 派工记录使用。
"""

from __future__ import annotations

import pytest

from longtask.adapters.registry import (
    ExecutorRegistry,
    LaunchSpec,
    RegistryEntry,
    _declared_model_from_argv,
    capabilities_from_dict,
)

pytestmark = pytest.mark.unit


def make_entry(**overrides: object) -> dict:
    data = {
        "id": "exec-a",
        "kind": "subprocess",
        "launch": {"argv": ["cli", "run", "--model", "m1", "{task}"], "env_allowlist": ["PATH"]},
        "capabilities": {},
        "models": ["m1"],
        "enabled": True,
    }
    data.update(overrides)
    return data


class TestDeclaredModelParsing:
    def test_space_form(self) -> None:
        assert _declared_model_from_argv(("cli", "--model", "m1", "{task}")) == "m1"

    def test_equals_form(self) -> None:
        assert _declared_model_from_argv(("cli", "--model=m2", "{task}")) == "m2"

    def test_absent(self) -> None:
        assert _declared_model_from_argv(("cli", "run", "{task}")) is None

    def test_trailing_flag_without_value_ignored(self) -> None:
        assert _declared_model_from_argv(("cli", "--model")) is None


class TestEntryValidation:
    def test_records_declared_model(self) -> None:
        entry = RegistryEntry.from_dict(make_entry())
        assert entry.declared_model == "m1"
        assert entry.effective_model() == "m1"

    def test_wildcard_without_declared_is_allowed(self) -> None:
        entry = RegistryEntry.from_dict(
            make_entry(models=["*"], launch={"argv": ["cli", "run", "{task}"], "env_allowlist": []})
        )
        assert entry.declared_model is None
        assert entry.effective_model() == "*"

    def test_wildcard_with_declared_records_it(self) -> None:
        entry = RegistryEntry.from_dict(make_entry(models=["*"]))
        assert entry.declared_model == "m1"
        assert entry.effective_model() == "m1"

    def test_rejects_declared_outside_whitelist(self) -> None:
        with pytest.raises(ValueError, match="not in models"):
            RegistryEntry.from_dict(make_entry(models=["m2"]))

    def test_rejects_specific_models_without_declared(self) -> None:
        with pytest.raises(ValueError, match="requires an explicit --model"):
            RegistryEntry.from_dict(
                make_entry(
                    models=["m1"],
                    launch={"argv": ["cli", "run", "{task}"], "env_allowlist": []},
                )
            )

    def test_executor_registry_rejects_bad_entry(self) -> None:
        with pytest.raises(ValueError):
            ExecutorRegistry.from_dict({"agents": [make_entry(models=["m2"])]})


class TestEffectiveModel:
    def _entry(self, models: tuple[str, ...], declared: str | None = None) -> RegistryEntry:
        return RegistryEntry(
            id="e",
            kind="subprocess",
            launch=LaunchSpec(),
            capabilities=capabilities_from_dict({}),
            models=models,
            declared_model=declared,
        )

    def test_prefers_declared(self) -> None:
        assert self._entry(("a", "b"), declared="b").effective_model() == "b"

    def test_falls_back_to_first_non_wildcard(self) -> None:
        assert self._entry(("a", "b")).effective_model() == "a"

    def test_falls_back_to_wildcard(self) -> None:
        assert self._entry(("*",)).effective_model() == "*"

    def test_to_dict_roundtrip(self) -> None:
        entry = RegistryEntry.from_dict(make_entry())
        dumped = entry.to_dict()
        assert dumped["declared_model"] == "m1"
