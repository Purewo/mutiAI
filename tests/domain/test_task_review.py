import pytest
from pydantic import ValidationError

from mutiai.domain import LeadReviewResult


def test_lead_review_accepts_only_bounded_structured_decisions() -> None:
    review = LeadReviewResult.model_validate(
        {
            "decision": "needs_revision",
            "final_summary": "  Revise the delivery evidence.  ",
            "issues": ["Missing test output."],
        }
    )

    assert review.final_summary == "Revise the delivery evidence."
    assert review.issues == ("Missing test output.",)
    schema = LeadReviewResult.model_json_schema()
    assert set(schema["required"]) == set(schema["properties"])

    with pytest.raises(ValidationError):
        LeadReviewResult.model_validate(
            {
                "decision": "maybe",
                "final_summary": "Unbounded decision.",
                "issues": [],
                "unexpected": True,
            }
        )

    with pytest.raises(ValidationError):
        LeadReviewResult.model_validate(
            {
                "decision": "accepted",
                "final_summary": "Missing the required issues field.",
            }
        )
