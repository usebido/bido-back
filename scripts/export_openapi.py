"""Write the API's OpenAPI document to openapi.json at the repo root.

    uv run python scripts/export_openapi.py [--check]

The output is deterministic (sorted keys, 2-space indent, trailing newline) so
the checked-in file diffs cleanly. `--check` exits non-zero when the file is
out of date, for CI.
"""

import argparse
import json
import sys
from pathlib import Path

from bido_back.api.app import create_app
from bido_back.settings import Settings

OUTPUT = Path(__file__).resolve().parents[1] / "openapi.json"


def render() -> str:
    # The schema does not depend on runtime configuration; placeholder settings
    # keep the export runnable without a .env or a database.
    settings = Settings.model_validate(
        {"environment": "local", "database_url": "postgresql://openapi@localhost:5432/unused"}
    )
    schema = create_app(settings).openapi()
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if openapi.json is stale")
    args = parser.parse_args()

    content = render()
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != content:
            print(f"{OUTPUT.name} is out of date; run scripts/export_openapi.py", file=sys.stderr)
            return 1
        return 0

    OUTPUT.write_text(content, encoding="utf-8")
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
