"""Injected semantic mappers are bounded and can only copy located source evidence."""

import asyncio
import json
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue

from bido_back.agents.context import ToolContext
from bido_back.purchasing.sources import mapping
from bido_back.purchasing.sources.contracts import (
    CellRef,
    ColumnMapping,
    EvidenceTable,
    FieldMapping,
    Issue,
    MappingProposal,
    MappingTask,
    RecordProposal,
    SourceRef,
    TableProposal,
)
from bido_back.purchasing.sources.mapping import map_sources, materialize
from bido_back.purchasing.sources.readers import SourceDocument, read_source
from tests.documents.fixtures import build_xlsx

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def context() -> ToolContext:
    return ToolContext(tenant_id=uuid4(), user_id=uuid4(), run_id=uuid4())


def columns(values: dict[str, int]) -> list[ColumnMapping]:
    return [ColumnMapping(field=field, column=column) for field, column in values.items()]


def fields(values: dict[str, CellRef]) -> list[FieldMapping]:
    return [FieldMapping(field=field, ref=ref) for field, ref in values.items()]


def test_model_proposal_schema_has_no_open_objects_or_dynamic_properties() -> None:
    # OpenAI strict outputs require additionalProperties=false. Provider schema
    # normalization can close structured objects, but cannot represent a dict's
    # arbitrary keys. Every nested proposal object must have declared fields.
    def check(schema: JsonValue) -> None:
        if isinstance(schema, dict):
            if schema.get("type") == "object":
                assert isinstance(schema.get("properties"), dict)
                assert schema.get("additionalProperties") in (None, False)
            for child in schema.values():
                check(child)
        elif isinstance(schema, list):
            for child in schema:
                check(child)

    check(MappingProposal.model_json_schema())


async def test_every_row_beyond_preview_is_mapped_with_cross_sheet_context() -> None:
    demand: list[list[object]] = [
        ["Planning export"],
        [],
        ["Ref. interna", "Qtde. requerida", "UM"],
        *[[f"P-{i:03d}", i + 1, "EA"] for i in range(128)],
    ]
    tables, read_issues = read_source(
        SourceDocument(
            "upload-1",
            "mixed.xlsx",
            XLSX,
            build_xlsx(
                {
                    "Demand": demand,
                    "Contacts": [["Vendor", "Legal name"], ["000123", "Example Supplier"]],
                    "Narrative": [["A note that is not a procurement record"]],
                }
            ),
        )
    )
    calls: list[MappingTask] = []

    async def mapper(task: MappingTask, _: ToolContext) -> MappingProposal:
        calls.append(task)
        if task.table == "Demand":
            return MappingProposal(
                sections=[
                    TableProposal(
                        kind="material_requirement",
                        header_row=3,
                        first_row=max(task.first_row, 4),
                        last_row=task.last_row,
                        columns=columns({"part_number": 1, "quantity": 2, "unit": 3}),
                    )
                ]
            )
        if task.table == "Contacts":
            return MappingProposal(
                sections=[
                    TableProposal(
                        kind="supplier",
                        header_row=1,
                        first_row=2,
                        last_row=2,
                        columns=columns({"supplier_code": 1, "name": 2}),
                    )
                ]
            )
        return MappingProposal()

    records, issues, coverage = await map_sources(
        tables, "What do I need to buy?", context(), mapper
    )

    assert read_issues == []
    needs = [record for record in records if record.kind == "material_requirement"]
    assert len(needs) == 128
    assert {record.values["part_number"] for record in needs} == {f"P-{i:03d}" for i in range(128)}
    assert needs[-1].values["quantity"] == "128"
    assert needs[-1].evidence["quantity"].row == 131
    pages = [task for task in calls if task.table == "Demand"]
    assert [(task.first_row, task.last_row) for task in pages] == [(1, 60), (61, 120), (121, 131)]
    assert pages[1].previous_sections[0].header_row == 3
    assert all(
        {part.name for part in task.manifest} == {"Demand", "Contacts", "Narrative"}
        for task in calls
    )
    assert {item.table: item.mapped_rows for item in coverage} == {
        "Demand": 128,
        "Contacts": 1,
        "Narrative": 0,
    }
    assert all(item.inspected for item in coverage)
    assert not any(issue.severity == "blocker" for issue in issues)
    assert any(issue.code == "unmapped_rows" and "Narrative" in issue.message for issue in issues)


@pytest.mark.parametrize("supplier_header", ["vendor", "supplier", "fornecedor"])
async def test_header_variants_use_the_injected_semantic_mapping(supplier_header: str) -> None:
    table = EvidenceTable(
        source_id="api-export",
        name="entities",
        rows=[[supplier_header, "Company"], ["000001", "Example Supplier"]],
    )

    async def mapper(task: MappingTask, _: ToolContext) -> MappingProposal:
        assert task.rows[0][0] == supplier_header
        # Models return only typed locators, never replacement names or quantities.
        return MappingProposal.model_validate_json(
            json.dumps(
                {
                    "sections": [
                        {
                            "kind": "supplier",
                            "header_row": 1,
                            "first_row": 2,
                            "last_row": 2,
                            "columns": [
                                {"field": "supplier_code", "column": 1},
                                {"field": "name", "column": 2},
                            ],
                        }
                    ],
                }
            )
        )

    records, _, coverage = await map_sources([table], "Read suppliers", context(), mapper)

    assert records[0].values == {"supplier_code": "000001", "name": "Example Supplier"}
    assert records[0].evidence["supplier_code"].source_id == "api-export"
    assert coverage[0].mapped_rows == 1


async def test_manifest_preserves_late_headers_and_separate_blocks_with_coordinates() -> None:
    table = EvidenceTable(
        source_id="erp",
        name="Opaque report",
        rows=[
            ["ERP export"],
            ["Date", "2026-10-08"],
            [],
            [],
            [],
            ["Finished product", "Component", "Consumption per 1"],
            ["MTR-100", "000101", "2"],
            [],
            ["Product", "Unit", "Area"],
            ["MTR-100", "pcs", "Motors"],
        ],
    )

    async def mapper(task: MappingTask, _: ToolContext) -> MappingProposal:
        manifest = task.manifest[0]
        anchored = dict(zip(manifest.sample_rows, manifest.sample, strict=True))
        assert anchored[6] == ["Finished product", "Component", "Consumption per 1"]
        assert anchored[9] == ["Product", "Unit", "Area"]
        return MappingProposal()

    _, issues, coverage = await map_sources([table], "Analyze motor", context(), mapper)
    assert coverage[0].inspected
    assert not any(issue.code == "mapping_failed" for issue in issues)


async def test_mapping_parallelism_is_bounded_and_context_stays_tenant_scoped() -> None:
    tables = [EvidenceTable(source_id=str(i), name="S", rows=[["Part"]]) for i in range(7)]
    original = context()
    active = 0
    peak = 0
    started = asyncio.Event()
    release = asyncio.Event()
    run_ids: set[UUID] = set()

    async def mapper(_: MappingTask, scoped: ToolContext) -> MappingProposal:
        nonlocal active, peak
        assert scoped.tenant_id == original.tenant_id
        assert scoped.user_id == original.user_id
        run_ids.add(scoped.run_id)
        active += 1
        peak = max(peak, active)
        if active == 4:
            started.set()
        try:
            await release.wait()
            return MappingProposal()
        finally:
            active -= 1

    pending = asyncio.create_task(map_sources(tables, "Read", original, mapper))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        assert peak == 4
    finally:
        release.set()
        _, _, coverage = await asyncio.wait_for(pending, timeout=2)

    assert peak == 4
    assert len(run_ids) == 7
    assert original.run_id not in run_ids
    assert len(coverage) == 7


@pytest.mark.parametrize(
    ("assigned", "expected_code"),
    [
        ({"secret": CellRef(row=2, column=1)}, "invalid_fields"),
        ({"part_number": CellRef(row=3, column=1)}, "invalid_locator"),
        ({"part_number": CellRef(row=2, column=3)}, "invalid_locator"),
        ({"part_number": CellRef(row=2, column=1, start=0)}, "invalid_slice"),
        ({"part_number": CellRef(row=2, column=1, start=0, end=100)}, "invalid_slice"),
        ({"part_number": CellRef(row=2, column=1, start=2, end=1)}, "invalid_slice"),
    ],
)
def test_materializer_rejects_invented_fields_and_invalid_evidence(
    assigned: dict[str, CellRef], expected_code: str
) -> None:
    table = EvidenceTable(source_id="S", name="T", rows=[["Part", "Qty"], ["P-01", "25"]])
    proposal = MappingProposal(
        records=[RecordProposal(kind="material_requirement", fields=fields(assigned))]
    )

    records, issues, covered = materialize(table, proposal, 1, 2)

    assert records == []
    assert covered == set()
    assert [issue.code for issue in issues] == [expected_code]


def test_valid_references_outside_the_current_page_are_rejected() -> None:
    table = EvidenceTable(source_id="S", name="T", rows=[["P-01"], ["P-02"], ["P-03"]])
    proposal = MappingProposal(
        records=[
            RecordProposal(
                kind="material_requirement",
                fields=fields({"part_number": CellRef(row=1, column=1)}),
            )
        ]
    )

    records, issues, covered = materialize(table, proposal, 2, 3)

    assert records == [] and covered == set()
    assert [issue.code for issue in issues] == ["invalid_locator"]


@pytest.mark.parametrize(
    "assigned", [{"part_number": 0}, {"part_number": 3}, {"part_number": 1, "quantity": 1}]
)
def test_invalid_table_column_assignments_are_rejected(assigned: dict[str, int]) -> None:
    table = EvidenceTable(source_id="S", name="T", rows=[["Part", "Qty"], ["P-01", "25"]])
    proposal = MappingProposal(
        sections=[
            TableProposal(
                kind="material_requirement",
                header_row=1,
                first_row=2,
                last_row=2,
                columns=columns(assigned),
            )
        ]
    )

    records, issues, covered = materialize(table, proposal, 1, 2)

    assert records == [] and covered == set()
    assert [issue.code for issue in issues] == ["invalid_mapping"]


@pytest.mark.parametrize("table_section", [False, True])
def test_duplicate_semantic_field_assignments_never_overwrite_each_other(
    table_section: bool,
) -> None:
    table = EvidenceTable(source_id="S", name="T", rows=[["Part", "Qty"], ["P-01", "25"]])
    if table_section:
        proposal = MappingProposal(
            sections=[
                TableProposal(
                    kind="material_requirement",
                    header_row=1,
                    first_row=2,
                    last_row=2,
                    columns=[
                        ColumnMapping(field="part_number", column=1),
                        ColumnMapping(field="part_number", column=2),
                    ],
                )
            ]
        )
    else:
        proposal = MappingProposal(
            records=[
                RecordProposal(
                    kind="material_requirement",
                    fields=[
                        FieldMapping(field="part_number", ref=CellRef(row=2, column=1)),
                        FieldMapping(field="part_number", ref=CellRef(row=2, column=2)),
                    ],
                )
            ]
        )

    records, issues, covered = materialize(table, proposal, 1, 2)

    assert records == [] and covered == set()
    assert [issue.code for issue in issues] == [
        "invalid_mapping" if table_section else "invalid_fields",
    ]


def test_text_record_values_are_copied_from_exact_source_spans() -> None:
    table = EvidenceTable(
        source_id="email-1",
        name="request.eml",
        layout="text",
        rows=[["Please buy P-01: 25 KG by 2026-10-31."]],
    )
    proposal = MappingProposal.model_validate_json(
        json.dumps(
            {
                "records": [
                    {
                        "kind": "material_requirement",
                        "fields": [
                            {
                                "field": "part_number",
                                "ref": {"row": 1, "column": 1, "start": 11, "end": 15},
                            },
                            {
                                "field": "quantity",
                                "ref": {"row": 1, "column": 1, "start": 17, "end": 19},
                            },
                            {
                                "field": "unit",
                                "ref": {"row": 1, "column": 1, "start": 20, "end": 22},
                            },
                            {
                                "field": "need_by",
                                "ref": {"row": 1, "column": 1, "start": 26, "end": 36},
                            },
                        ],
                    }
                ],
            }
        )
    )

    records, issues, covered = materialize(table, proposal, 1, 1)

    assert issues == []
    assert covered == {1}
    assert records[0].values == {
        "part_number": "P-01",
        "quantity": "25",
        "unit": "KG",
        "need_by": "2026-10-31",
    }
    assert records[0].evidence["quantity"].model_dump() == {
        "source_id": "email-1",
        "table": "request.eml",
        "row": 1,
        "column": 1,
        "start": 17,
        "end": 19,
    }


def test_duplicate_semantic_records_are_not_double_counted() -> None:
    table = EvidenceTable(source_id="S", name="T", rows=[["Part"], ["P-01"]])
    section = TableProposal(
        kind="material_requirement",
        header_row=1,
        first_row=2,
        last_row=2,
        columns=columns({"part_number": 1}),
    )

    records, issues, covered = materialize(
        table, MappingProposal(sections=[section, section]), 1, 2
    )

    assert len(records) == 1
    assert covered == {2}
    assert [issue.code for issue in issues] == ["overlapping_mapping"]


async def test_mapper_failure_does_not_hide_other_source_results_or_leak_payloads() -> None:
    tables = [EvidenceTable(source_id=name, name=name, rows=[["P-01"]]) for name in ["bad", "ok"]]

    async def mapper(task: MappingTask, _: ToolContext) -> MappingProposal:
        if task.table == "bad":
            raise ValueError("private source content")
        return MappingProposal(
            records=[
                RecordProposal(
                    kind="material_requirement",
                    fields=fields({"part_number": CellRef(row=1, column=1)}),
                )
            ]
        )

    records, issues, coverage = await map_sources(tables, "Read", context(), mapper)

    assert len(records) == 1 and records[0].evidence["part_number"].table == "ok"
    assert {item.table: item.inspected for item in coverage} == {"bad": False, "ok": True}
    assert any(issue.code == "mapping_failed" for issue in issues)
    assert all("private" not in issue.message for issue in issues)


@pytest.mark.parametrize("limit", ["pages", "row_chars"])
async def test_interpretation_limits_reject_before_any_model_call(
    monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    tables = [EvidenceTable(source_id="S", name="T", rows=[["Part"], ["P-01"]])]
    if limit == "pages":
        monkeypatch.setattr(mapping, "PAGE_ROWS", 1)
        monkeypatch.setattr(mapping, "MAX_PAGES", 1)
    else:
        monkeypatch.setattr(mapping, "PAGE_CHARS", 3)
    calls = 0

    async def mapper(_: MappingTask, __: ToolContext) -> MappingProposal:
        nonlocal calls
        calls += 1
        return MappingProposal()

    records, issues, coverage = await map_sources(tables, "Read", context(), mapper)

    assert calls == 0 and records == []
    assert [issue.code for issue in issues] == ["source_limit"]
    assert coverage[0].rows == 2 and not coverage[0].inspected


@pytest.mark.parametrize(
    ("first_ref", "second_ref"),
    [
        (CellRef(row=1, column=1), CellRef(row=1, column=1)),
        (CellRef(row=1, column=1), CellRef(row=1, column=1, start=2, end=4)),
        (CellRef(row=1, column=1, start=0, end=3), CellRef(row=1, column=1, start=2, end=4)),
    ],
)
def test_distinct_fields_cannot_reuse_overlapping_source_evidence(
    first_ref: CellRef,
    second_ref: CellRef,
) -> None:
    table = EvidenceTable(source_id="S", name="T", rows=[["00125"]])
    proposal = MappingProposal(
        records=[
            RecordProposal(
                kind="material_requirement",
                fields=fields({"part_number": first_ref, "quantity": second_ref}),
            )
        ]
    )

    records, issues, covered = materialize(table, proposal, 1, 1)

    assert records == [] and covered == set()
    assert [issue.code for issue in issues] == ["overlapping_fields"]


def test_adjacent_nonoverlapping_text_fields_remain_usable() -> None:
    table = EvidenceTable(source_id="S", name="T", layout="text", rows=[["00125"]])
    proposal = MappingProposal(
        records=[
            RecordProposal(
                kind="material_requirement",
                fields=fields(
                    {
                        "part_number": CellRef(row=1, column=1, start=0, end=3),
                        "quantity": CellRef(row=1, column=1, start=3, end=5),
                    }
                ),
            )
        ]
    )

    records, issues, _ = materialize(table, proposal, 1, 1)

    assert issues == []
    assert records[0].values == {"part_number": "001", "quantity": "25"}


@pytest.mark.parametrize(
    "ref",
    [
        SourceRef(source_id="other", table="T", row=1, column=1),
        SourceRef(source_id="S", table="other", row=1, column=1),
        SourceRef(source_id="S", table="T", row=3, column=1),
        SourceRef(source_id="S", table="T", row=1, column=2),
        SourceRef(source_id="S", table="T", row=1, column=1, start=0, end=20),
        SourceRef(source_id="S", table="T", row=1, column=1, start=0),
    ],
)
def test_model_issue_evidence_must_reference_the_actual_source_page(ref: SourceRef) -> None:
    table = EvidenceTable(source_id="S", name="T", rows=[["P-01"]])
    issue = Issue(code="uncertain_quantity", severity="warning", message="Check", evidence=[ref])

    records, issues, covered = materialize(table, MappingProposal(issues=[issue]), 1, 1)

    assert records == [] and covered == set()
    assert [issue.code for issue in issues] == ["invalid_issue_locator"]
    assert issues[0].severity == "blocker"
    assert issues[0].evidence == []


def test_grounded_model_issue_is_retained_with_its_evidence() -> None:
    table = EvidenceTable(source_id="S", name="T", rows=[["Quantity unclear"]])
    issue = Issue(
        code="uncertain_quantity",
        severity="blocker",
        message="Quantity requires review",
        evidence=[SourceRef(source_id="S", table="T", row=1, column=1, start=9, end=16)],
    )

    _, issues, covered = materialize(table, MappingProposal(issues=[issue]), 1, 1)

    assert issues == [issue]
    assert covered == set()


async def test_global_manifest_sample_is_bounded_without_truncating_page_evidence() -> None:
    tables = [
        EvidenceTable(
            source_id="S",
            name=f"T-{i}",
            rows=[["x" * 120 for _ in range(40)] for _ in range(4)],
        )
        for i in range(30)
    ]
    calls: list[MappingTask] = []

    async def mapper(task: MappingTask, _: ToolContext) -> MappingProposal:
        calls.append(task)
        return MappingProposal()

    _, _, coverage = await map_sources(tables, "Read", context(), mapper)

    assert len(calls) == 30
    assert len(coverage) == 30
    for task in calls:
        sampled = sum(
            len(value or "") for table in task.manifest for row in table.sample for value in row
        )
        assert sampled <= mapping.MANIFEST_CHARS
        assert len(task.manifest) == 30
        assert task.rows[0][0] == "x" * 120
        assert all(table.rows == 4 and table.columns == 40 for table in task.manifest)


async def test_many_empty_tables_cannot_bypass_the_interpretation_budget() -> None:
    tables = [
        EvidenceTable(source_id="S", name=f"T-{i}", rows=[]) for i in range(mapping.MAX_TABLES + 1)
    ]
    calls = 0

    async def mapper(_: MappingTask, __: ToolContext) -> MappingProposal:
        nonlocal calls
        calls += 1
        return MappingProposal()

    records, issues, coverage = await map_sources(tables, "Read", context(), mapper)

    assert calls == 0 and records == []
    assert [issue.code for issue in issues] == ["source_limit"]
    assert len(coverage) == len(tables)
    assert all(not item.inspected and item.mapped_rows == 0 for item in coverage)


def test_a_section_running_past_the_page_is_read_up_to_the_page() -> None:
    table = EvidenceTable(
        source_id="S",
        name="T",
        rows=[["Part", "Qty"], ["P-01", "25"], ["P-02", "30"], ["P-03", "35"]],
    )
    proposal = MappingProposal(
        sections=[
            TableProposal(
                kind="material_requirement",
                header_row=1,
                first_row=2,
                last_row=9,  # past the page and the table: cut, not rejected
                columns=columns({"part_number": 1, "quantity": 2}),
            )
        ]
    )

    records, issues, covered = materialize(table, proposal, 1, 3)

    assert [r.values["part_number"] for r in records] == ["P-01", "P-02"]
    assert covered == {2, 3}
    assert issues == []


async def test_a_page_with_a_rejected_section_is_asked_once_more_with_the_reason() -> None:
    table = EvidenceTable(
        source_id="S", name="T", rows=[["Part", "Qty"], ["P-01", "25"], ["P-02", "30"]]
    )
    seen: list[list[str]] = []

    async def mapper(task: MappingTask, _: ToolContext) -> MappingProposal:
        seen.append(task.retry_feedback)
        # First answer maps two fields to one column; the corrected one does not.
        assigned = (
            {"part_number": 1, "quantity": 1}
            if not task.retry_feedback
            else {
                "part_number": 1,
                "quantity": 2,
            }
        )
        return MappingProposal(
            sections=[
                TableProposal(
                    kind="material_requirement",
                    header_row=1,
                    first_row=2,
                    last_row=3,
                    columns=columns(assigned),
                )
            ]
        )

    records, issues, coverage = await map_sources([table], "Read", context(), mapper)

    assert len(seen) == 2 and seen[0] == []
    assert any("same field twice" in item or "same column" in item for item in seen[1])
    assert [r.values["quantity"] for r in records] == ["25", "30"]
    assert not any(issue.code == "invalid_mapping" for issue in issues)
    assert coverage[0].mapped_rows == 2


async def test_a_retry_that_is_no_better_keeps_the_first_answer() -> None:
    table = EvidenceTable(
        source_id="S", name="T", rows=[["Part", "Qty"], ["P-01", "25"], ["P-02", "30"]]
    )
    calls = 0

    async def mapper(task: MappingTask, _: ToolContext) -> MappingProposal:
        nonlocal calls
        calls += 1
        sections = [
            TableProposal(
                kind="material_requirement",
                header_row=1,
                first_row=2,
                last_row=3,
                columns=columns({"part_number": 1}),
            )
        ]
        if not task.retry_feedback:
            sections.append(
                TableProposal(
                    kind="inventory",
                    header_row=1,
                    first_row=2,
                    last_row=3,
                    columns=columns({"part_number": 7}),
                )
            )
        else:
            sections = []  # a worse second answer
        return MappingProposal(sections=sections)

    records, issues, _ = await map_sources([table], "Read", context(), mapper)

    assert calls == 2
    assert [r.values["part_number"] for r in records] == ["P-01", "P-02"]
    assert [i.code for i in issues if i.code == "invalid_mapping"] == ["invalid_mapping"]
