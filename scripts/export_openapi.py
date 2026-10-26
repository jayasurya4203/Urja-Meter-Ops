"""Write app.openapi() to openapi.json at the repository root.

Run from the repository root:

    python scripts/export_openapi.py

The app is imported without starting its lifespan, so no portal call is made.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.main import app  # noqa: E402


def main() -> None:
    output = ROOT / "openapi.json"
    spec = app.openapi()
    output.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {output} ({len(spec.get('paths', {}))} paths, OpenAPI {spec['openapi']})")


if __name__ == "__main__":
    main()
