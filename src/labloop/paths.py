"""Study / workdir layout per DEVELOPMENT.md §7."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from labloop.errors import INVALID_ARGUMENT, AmbiguousStudyError, LabloopError

DEFAULT_WORKDIR_NAME = ".labloop"
_WORD_RE = re.compile(r"[a-z0-9]+")
ASCII_MIN_LENGTH = 3
MAX_SLUG_WORDS = 6
MAX_STUDY_NAME_LENGTH = 64

#: Characters that are illegal in a Windows path segment (and ambiguous elsewhere).
_ILLEGAL_NAME_CHARS = set('<>:"/\\|?*')
#: Reserved device names on Windows (usable as file names via NT namespaces, not via paths).
_RESERVED_NAMES = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}


def validate_study_name(name: str) -> str:
    """A study name must be a single, safe path segment (no traversal, no separators).

    The name becomes a directory under ``<workdir>/studies/``; anything that could
    escape that directory is rejected with a structured error instead of silently
    writing somewhere else.
    """
    if not name or name != name.strip():
        raise LabloopError(
            f"study 名称不能为空或带首尾空白：{name!r}",
            code=INVALID_ARGUMENT,
            details={"name": name},
        )
    if len(name) > MAX_STUDY_NAME_LENGTH:
        raise LabloopError(
            f"study 名称过长（>{MAX_STUDY_NAME_LENGTH} 字符）：{name!r}",
            code=INVALID_ARGUMENT,
            details={"name": name},
        )
    if name in {".", ".."}:
        raise LabloopError(
            f"study 名称不能是路径符号：{name!r}",
            code=INVALID_ARGUMENT,
            details={"name": name},
        )
    illegal = sorted({char for char in name if char in _ILLEGAL_NAME_CHARS or ord(char) < 32})
    if illegal:
        raise LabloopError(
            f'study 名称含非法字符 {"".join(illegal)}（不能包含路径分隔符或 <>:"/|?*）',
            code=INVALID_ARGUMENT,
            details={"name": name, "illegal": illegal},
        )
    if name.endswith("."):
        raise LabloopError(
            f"study 名称不能以「.」结尾（Windows 不支持）：{name!r}",
            code=INVALID_ARGUMENT,
            details={"name": name},
        )
    stem = name.split(".")[0].lower()
    if stem in _RESERVED_NAMES:
        raise LabloopError(
            f"study 名称是 Windows 保留名：{name!r}",
            code=INVALID_ARGUMENT,
            details={"name": name},
        )
    return name


def slugify(question: str, *, taken: set[str] | None = None) -> str:
    """ASCII slug from the question; falls back to a short hash for CJK-only text."""
    taken = taken or set()
    words = _WORD_RE.findall(question.lower())
    candidate = "-".join(words[:MAX_SLUG_WORDS])
    if len(candidate) < ASCII_MIN_LENGTH:
        candidate = f"study-{hashlib.sha1(question.encode('utf-8')).hexdigest()[:8]}"
    slug = candidate
    index = 2
    while slug in taken:
        slug = f"{candidate}-{index}"
        index += 1
    return slug


def resolve_workdir(explicit: str | None) -> Path:
    root = Path(explicit) if explicit else Path.cwd() / DEFAULT_WORKDIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def round_name(round_no: int) -> str:
    return f"round-{round_no:03d}"


@dataclass
class StudyLayout:
    root: Path  # <workdir>/studies/<slug>

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def slug(self) -> str:
        return self.root.name

    @property
    def study_json(self) -> Path:
        return self.root / "study.json"

    @property
    def recipe_json(self) -> Path:
        return self.root / "recipe.json"

    @property
    def rounds_dir(self) -> Path:
        path = self.root / "rounds"
        path.mkdir(exist_ok=True)
        return path

    def round_dir(self, round_no: int) -> Path:
        path = self.rounds_dir / round_name(round_no)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def plan_path(self, round_no: int) -> Path:
        return self.round_dir(round_no) / "plan.json"

    def plan_path_for(self, round_no: int) -> Path:
        """plan.json path *without* creating any directory (existence checks)."""
        return self.root / "rounds" / round_name(round_no) / "plan.json"

    def analysis_path(self, round_no: int) -> Path:
        return self.round_dir(round_no) / "analysis.json"

    def runs_dir(self, round_no: int) -> Path:
        path = self.round_dir(round_no) / "runs"
        path.mkdir(exist_ok=True)
        return path

    def run_dir(self, round_no: int, run_id: str) -> Path:
        path = self.runs_dir(round_no) / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def report_json_path(self) -> Path:
        return self.root / "report.json"

    @property
    def report_html_path(self) -> Path:
        return self.root / "report.html"

    def next_round_number(self) -> int:
        numbers = [int(path.name.removeprefix("round-")) for path in self.rounds_dir.glob("round-*") if path.is_dir()]
        return max(numbers, default=0) + 1

    def round_numbers(self) -> list[int]:
        return sorted(
            int(path.name.removeprefix("round-")) for path in self.rounds_dir.glob("round-*") if path.is_dir()
        )


@dataclass
class WorkLayout:
    root: Path

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def studies_dir(self) -> Path:
        path = self.root / "studies"
        path.mkdir(exist_ok=True)
        return path

    def study(self, slug: str) -> StudyLayout:
        return StudyLayout(self.studies_dir / slug)

    def existing_slugs(self) -> set[str]:
        return {path.name for path in self.studies_dir.iterdir() if path.is_dir()}

    def find_study(self, name: str) -> StudyLayout | None:
        """Resolve a study by exact slug first, then by unique prefix."""
        exact = self.studies_dir / name
        if (exact / "study.json").is_file():
            return StudyLayout(exact)
        matches = [
            StudyLayout(path)
            for path in self.studies_dir.iterdir()
            if path.is_dir() and path.name.startswith(name) and (path / "study.json").is_file()
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            candidates = sorted(layout.slug for layout in matches)
            raise AmbiguousStudyError(
                f"study 名「{name}」匹配到多个：{', '.join(candidates)}",
                details={"name": name, "candidates": candidates},
            )
        return None
