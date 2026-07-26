import json
from pathlib import Path

from mutiai.api.errors import ErrorEnvelope
from mutiai.api.schemas.feasibility import FeasibilityCheckResponse
from mutiai.api.schemas.runtime_bindings import RuntimeBindingResponse

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = PROJECT_ROOT / "contracts" / "fixtures" / "feasibility"


def load(name: str):
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def test_committed_feasibility_fixtures_match_public_models() -> None:
    RuntimeBindingResponse.model_validate(
        load("runtime-binding-linux-standard.json")
    )
    FeasibilityCheckResponse.model_validate(
        load("organization-blocked-check.zh-CN.json")
    )
    ErrorEnvelope.model_validate(load("organization-blocked-error.zh-CN.json"))
    ErrorEnvelope.model_validate(
        load("task-capability-unknown-error.zh-CN.json")
    )
    FeasibilityCheckResponse.model_validate(
        load("task-capability-unknown-check.zh-CN.json")
    )
    for check in load("task-feasible-checks.zh-CN.json"):
        FeasibilityCheckResponse.model_validate(check)
