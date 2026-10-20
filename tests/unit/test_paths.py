"""Study name validation (path-traversal guard) and slug generation."""

from __future__ import annotations

import pytest

from labloop.errors import INVALID_ARGUMENT, LabloopError
from labloop.paths import slugify, validate_study_name


def test_validate_study_name_accepts_normal_names() -> None:
    for name in ["batch", "batch-smoke", "batch_2", "study-2026", "实验", "a.b"]:
        assert validate_study_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        " ",
        " batch",
        "batch ",
        "..",
        ".",
        "..\\escaped",
        "../escaped",
        "sub/study",
        "sub\\study",
        "C:\\abs",
        "con",
        "NUL.txt",
        "batch.",
        "bad<name>",
        "a|b",
        "x" * 65,
    ],
)
def test_validate_study_name_rejects_unsafe_names(name: str) -> None:
    with pytest.raises(LabloopError) as excinfo:
        validate_study_name(name)
    assert excinfo.value.code == INVALID_ARGUMENT


def test_slugify_is_always_a_safe_name() -> None:
    assert validate_study_name(slugify("batch size 对精度的影响")) == "batch-size"
    # 纯中文问题 → 哈希 slug，仍然是安全目录名
    assert validate_study_name(slugify("批量大小对精度的影响"))
    assert slugify("batch", taken={"batch"}) == "batch-2"
