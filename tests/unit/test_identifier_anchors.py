"""标识符校验的锚点边界：`$` 会匹配「尾随换行之前」的位置（DESIGN §14.2）。

Python 的 ``$`` 不等于「串尾」：``re.match(r"^ab$", "ab\\n")`` **是匹配的**。
校验函数的 docstring 越强调 fail-closed，这种「接受了一个本不该接受的值」就越
不能当风格问题看——它是自称的保证与行为不符。

正确写法二选一：``re.fullmatch``，或 ``re.match`` + ``\\Z``（本仓库统一用后者，
改动最小且语义显式）。新增校验器时照抄这里的一条用例。
"""

from __future__ import annotations

import pytest

from lhgp.persistence.resume import ResumeBriefError, build_resume_brief
from lhgp.rpc.handlers._common import _is_safe_contract_id as canonical_is_safe_contract_id
from lhgp.untrusted.boundary import make_untrusted_block
from longtask.rpc.handlers._common import _is_safe_contract_id as legacy_is_safe_contract_id

_TRAILING_NEWLINE_FORMS = ("ab\n", "ab\r\n", "lt-a\n")


class TestUntrustedBlockLabel:
    def test_trailing_newline_is_rejected(self) -> None:
        """标签进围栏首行；尾随换行会在块内凭空多出一行。"""
        with pytest.raises(ValueError, match="invalid untrusted-block label"):
            make_untrusted_block(label="ab\n", title="T", body="x")

    def test_trailing_crlf_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid untrusted-block label"):
            make_untrusted_block(label="ab\r\n", title="T", body="x")

    def test_plain_label_is_still_accepted(self) -> None:
        """收紧只针对尾随换行，正常标签不受影响。"""
        assert "label=last-handover" in make_untrusted_block(
            label="last-handover", title="T", body="x"
        )


class TestResumePathComponents:
    @pytest.mark.parametrize("bad", _TRAILING_NEWLINE_FORMS)
    def test_attempt_id_with_trailing_newline_is_rejected(self, tmp_path, bad: str) -> None:
        with pytest.raises(ResumeBriefError, match="attempt_id must match"):
            build_resume_brief(tmp_path, "lt-a", bad)

    def test_contract_id_with_trailing_newline_is_rejected(self, tmp_path) -> None:
        with pytest.raises(ResumeBriefError, match="contract_id must match"):
            build_resume_brief(tmp_path, "lt-a\n", "att-1")


class TestContractIdGuard:
    @pytest.mark.parametrize("bad", _TRAILING_NEWLINE_FORMS)
    def test_canonical_copy_rejects_trailing_newline(self, bad: str) -> None:
        assert canonical_is_safe_contract_id(bad) is False

    @pytest.mark.parametrize("bad", _TRAILING_NEWLINE_FORMS)
    def test_legacy_copy_rejects_trailing_newline(self, bad: str) -> None:
        """两棵树各有一份同名正则；只修一棵等于留一条后门。"""
        assert legacy_is_safe_contract_id(bad) is False

    def test_dot_segments_are_still_rejected(self) -> None:
        assert canonical_is_safe_contract_id("..") is False
        assert canonical_is_safe_contract_id("lt-..-a") is False

    def test_plain_contract_id_is_still_accepted(self) -> None:
        assert canonical_is_safe_contract_id("lt-20260910-001") is True
