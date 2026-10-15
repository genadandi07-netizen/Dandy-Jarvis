"""Generate OPENAPI_SPEC.json from the FastAPI app and Pydantic models."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pydantic import TypeAdapter  # noqa: E402

from gateway.app import app  # noqa: E402
from gateway.contracts import (  # noqa: E402
    AmoLeadCreateSchema,
    OneCCounterpartySchema,
    OneCOrderCreateSchema,
    WebhookPayload,
)
from gateway.errors import ErrorShape  # noqa: E402

OUTPUT = ROOT / "OPENAPI_SPEC.json"

MODELS = (
    WebhookPayload,
    AmoLeadCreateSchema,
    OneCOrderCreateSchema,
    OneCCounterpartySchema,
)


def _hoist_defs(schema: dict, schemas: dict) -> dict:
    """Move nested ``$defs`` up into ``components/schemas``.

    Pydantic v2 emits nested models under a schema-local ``$defs`` block, but
    the ``ref_template`` renders their ``$ref`` targets as
    ``#/components/schemas/<name>``. Hoisting every ``$defs`` entry to the
    top-level keeps those references resolvable.
    """
    for name, sub in schema.pop("$defs", {}).items():
        schemas.setdefault(name, _hoist_defs(sub, schemas))
    return schema


def build_spec() -> dict:
    spec = app.openapi()
    components = spec.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    for model in MODELS:
        schema = _hoist_defs(
            model.model_json_schema(ref_template="#/components/schemas/{model}"),
            schemas,
        )
        schemas[model.__name__] = schema
    schemas["ErrorShape"] = TypeAdapter(ErrorShape).json_schema(
        ref_template="#/components/schemas/{model}"
    )
    return spec


def main() -> None:
    spec = build_spec()
    OUTPUT.write_text(
        json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()