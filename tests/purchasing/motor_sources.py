"""The synthetic scattered motor evidence as a workbook, and its known mapping."""

from collections.abc import Sequence

from bido_back.agents.context import ToolContext
from bido_back.purchasing.sources.contracts import (
    ColumnMapping,
    MappingProposal,
    MappingTask,
    Record,
    RecordKind,
    TableProposal,
)
from evals.source_fixtures import motor_records
from tests.purchasing.test_workbook import workbook


class MotorMapper:
    """Maps the one-table-per-kind workbook of `motor_workbook`, counting model calls."""

    def __init__(self, sections: dict[str, TableProposal]) -> None:
        self.sections = sections
        self.calls = 0

    async def __call__(self, task: MappingTask, context: ToolContext) -> MappingProposal:
        self.calls += 1
        section = self.sections[task.table]
        if section.last_row < task.first_row or section.first_row > task.last_row:
            return MappingProposal()
        return MappingProposal(
            sections=[
                section.model_copy(
                    update={
                        "first_row": max(section.first_row, task.first_row),
                        "last_row": min(section.last_row, task.last_row),
                    }
                )
            ]
        )


def motor_workbook(records: Sequence[Record] | None = None) -> tuple[bytes, MotorMapper]:
    """The synthetic scattered motor evidence as a workbook, one sheet per record kind."""
    rows = motor_records() if records is None else list(records)
    sheets: dict[str, list[list[object]]] = {}
    sections: dict[str, TableProposal] = {}
    kinds: list[RecordKind] = list(dict.fromkeys(r.kind for r in rows))
    for kind in kinds:
        matching = [r for r in rows if r.kind == kind]
        fields = list(dict.fromkeys(f for r in matching for f in r.values))
        sheets[kind] = [list(fields), *([r.values.get(f) for f in fields] for r in matching)]
        sections[kind] = TableProposal(
            kind=kind,
            header_row=1,
            first_row=2,
            last_row=len(matching) + 1,
            columns=[ColumnMapping(field=f, column=i) for i, f in enumerate(fields, 1)],
        )
    return workbook(sheets), MotorMapper(sections)
