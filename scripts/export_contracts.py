"""Export versioned JSON Schema snapshots from product-domain models."""

from __future__ import annotations

import json
from pathlib import Path

from mutiai.domain import OrganizationSpec


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ORGANIZATION_SCHEMA_PATH = (
    PROJECT_ROOT / "contracts" / "schemas" / "organization-spec.v1.json"
)


def main() -> None:
    schema = OrganizationSpec.model_json_schema()
    ORGANIZATION_SCHEMA_PATH.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {ORGANIZATION_SCHEMA_PATH}")


if __name__ == "__main__":
    main()

