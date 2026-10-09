"""What an RFQ line drafted from a procurement assessment rests on.

The assessed line is kept in the RFQ line's `source_evidence["line"]`; its confidence
and assumptions are read from there for the RFQ view and the send approval. Lines not
drafted from an assessment have neither.
"""

from collections.abc import Mapping

from pydantic import BaseModel, Field, JsonValue, ValidationError

from bido_back.rfqs.schemas import LineConfidence, RfqLineAssumption


class _Assessed(BaseModel):
    confidence: LineConfidence | None = None
    assumptions: list[RfqLineAssumption] = Field(default_factory=list[RfqLineAssumption])


def line_assumptions(
    source_evidence: Mapping[str, JsonValue] | None,
) -> tuple[LineConfidence | None, list[RfqLineAssumption]]:
    line = (source_evidence or {}).get("line")
    if not isinstance(line, dict):
        return None, []
    try:
        assessed = _Assessed.model_validate(line)
    except ValidationError:
        return None, []
    return assessed.confidence, assessed.assumptions
