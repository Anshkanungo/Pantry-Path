"""docs/result-v1.schema.json must match the Result model, so teammates' types stay true."""

import json
from pathlib import Path

from pantrypath_core.result import Result

SCHEMA_FILE = Path(__file__).resolve().parents[2] / "docs" / "result-v1.schema.json"
REGENERATE = "python shared/tests/test_contract_schema.py  (from the repo root)"


def current_schema() -> dict:
    schema = Result.model_json_schema(mode="serialization")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "Pantry Path result.json v1.0"
    schema["description"] = (
        "Output of the Pantry Path data pipeline for one request. Generated from "
        "pantrypath_core.result.Result; regenerate instead of editing by hand."
    )
    return schema


def test_committed_schema_matches_model():
    committed = json.loads(SCHEMA_FILE.read_text())
    assert committed == current_schema(), f"Schema is stale; regenerate with: {REGENERATE}"


if __name__ == "__main__":
    SCHEMA_FILE.write_text(json.dumps(current_schema(), indent=2) + "\n")
    print(f"wrote {SCHEMA_FILE}")
