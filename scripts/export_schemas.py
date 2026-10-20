"""Export the pydantic model JSON schemas to docs/schemas/.

Usage:
    python scripts/export_schemas.py          # write docs/schemas/*.json
    python scripts/export_schemas.py --check  # exit 1 when the files are stale
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from labloop.domain.models import (  # noqa: E402
    ExperimentPlan,
    Report,
    RoundAnalysis,
    RunResult,
    Study,
)

SCHEMAS = {
    "study.schema.json": Study,
    "experiment_plan.schema.json": ExperimentPlan,
    "run_result.schema.json": RunResult,
    "round_analysis.schema.json": RoundAnalysis,
    "report.schema.json": Report,
}


def render(model: type) -> str:
    schema = model.model_json_schema()
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    check_only = "--check" in sys.argv[1:]
    out_dir = ROOT / "docs" / "schemas"
    out_dir.mkdir(parents=True, exist_ok=True)
    stale: list[str] = []
    for name, model in SCHEMAS.items():
        path = out_dir / name
        content = render(model)
        if check_only:
            current = path.read_text(encoding="utf-8") if path.is_file() else ""
            if current != content:
                stale.append(name)
        else:
            path.write_text(content, encoding="utf-8", newline="\n")
            print(f"wrote {path.relative_to(ROOT)}")
    if check_only:
        if stale:
            print(f"stale schemas: {', '.join(stale)} (run python scripts/export_schemas.py)")
            return 1
        print("schemas are current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
