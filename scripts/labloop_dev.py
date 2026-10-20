"""Dev entry point: run the CLI from a source checkout without an editable install.

Why this exists is a real Windows finding inherited from LabBox (DEVELOPMENT.md §22):
an editable install writes a ``.pth`` file containing the absolute source path, and
``site.py`` decodes ``.pth`` files with the *locale* encoding. On a cp936 machine whose
project path contains non-ASCII characters (``C:\\code\\github小项目``) that path comes
out mojibake and ``import labloop`` fails, so the ``labloop.exe`` console script cannot
start. For users the workaround is installing under an ASCII path (pipx / uv tool install).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from labloop.cli.main import app

if __name__ == "__main__":
    app()
