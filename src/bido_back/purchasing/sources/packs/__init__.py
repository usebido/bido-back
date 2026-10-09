"""Report packs: known ERP reports read by code, before (and instead of) the mapper.

A table a pack recognizes is read without a model call; the others go to the source
mapper as before. Another ERP (TOTVS, Oracle) is one more `ReportPack` in `PACKS`.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from bido_back.purchasing.sources.contracts import (
    Assumption,
    Coverage,
    EvidenceTable,
    Issue,
    RecognizedReport,
    Record,
)
from bido_back.purchasing.sources.packs.base import PackContext, Reading, ReportPack
from bido_back.purchasing.sources.packs.sap import SAP

PACKS: tuple[ReportPack, ...] = (SAP,)


@dataclass
class PackResult:
    records: list[Record]
    issues: list[Issue]
    assumptions: list[Assumption]
    coverage: list[Coverage]
    reports: list[RecognizedReport]
    # Tables no pack recognized: the source mapper reads them.
    remaining: list[EvidenceTable]


def read_known(tables: Sequence[EvidenceTable], context: PackContext) -> PackResult:
    readings: dict[str, list[Reading]] = {pack.name: [] for pack in PACKS}
    remaining: list[EvidenceTable] = []
    for table in tables:
        for pack in PACKS:
            recognition = pack.recognize(table)
            if recognition is not None:
                readings[pack.name].append(pack.read(table, recognition, context))
                break
        else:
            remaining.append(table)
    issues: list[Issue] = []
    assumptions: list[Assumption] = []
    for pack in PACKS:
        found, settled = pack.reconcile(readings[pack.name], context)
        issues.extend(found)
        assumptions.extend(settled)
    every = [(pack.name, r) for pack in PACKS for r in readings[pack.name]]
    for _, reading in every:
        issues.extend(reading.issues)
        assumptions.extend(reading.assumptions)
    return PackResult(
        records=[record for _, reading in every for record in reading.records],
        issues=issues,
        assumptions=assumptions,
        coverage=[
            Coverage(
                source_id=reading.table.source_id,
                filename=reading.table.source_id,
                table=reading.table.name,
                rows=len(reading.table.rows),
                mapped_rows=len(reading.covered),
                inspected=True,
            )
            for _, reading in every
        ],
        reports=[
            RecognizedReport(
                source_id=reading.table.source_id,
                table=reading.table.name,
                pack=name,
                report=reading.report,
                rows=len(reading.covered),
            )
            for name, reading in every
        ],
        remaining=remaining,
    )
