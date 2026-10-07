"""The pure checklist builder: scopes, status, expiry, candidates, summaries."""

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from bido_back.compliance.checklist import (
    DocumentRecord,
    LineInfo,
    ReceivedAttachment,
    SupplierInfo,
    add_months,
    build_items,
    filename_matches,
    summarize,
)
from bido_back.compliance.defaults import DEFAULT_REQUIREMENTS
from bido_back.compliance.schemas import ChecklistItem, Requirement

TODAY = date(2026, 10, 7)
RFQ = uuid4()
AEC_LINE = LineInfo(
    id=uuid4(), rfq_id=RFQ, position=1, mpn="GCM188R71H104KA57D", qualification="AEC-Q200"
)
PLAIN_LINE = LineInfo(id=uuid4(), rfq_id=RFQ, position=2, mpn=None, qualification=None)
DISTRIBUTOR = SupplierInfo(id=uuid4(), name="Rhein", type="authorized_distributor")
MANUFACTURER = SupplierInfo(id=uuid4(), name="Murata", type="manufacturer")


def at(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 9, tzinfo=UTC)


def doc(
    code: str,
    *,
    line: UUID | None = None,
    attachment: UUID | None = None,
    issued: date | None = None,
    expires: date | None = None,
    recorded: date = date(2026, 9, 1),
) -> DocumentRecord:
    return DocumentRecord(
        id=uuid4(),
        requirement_code=code,
        rfq_line_id=line,
        attachment_id=attachment,
        filename="file.pdf" if attachment else None,
        url=None if attachment else "https://supplier.example.test/doc.pdf",
        issued_on=issued,
        expires_on=expires,
        recorded_at=at(recorded),
    )


def items_for(
    supplier: SupplierInfo = DISTRIBUTOR,
    documents: list[DocumentRecord] | None = None,
    attachments: list[ReceivedAttachment] | None = None,
    lines: list[LineInfo] | None = None,
) -> list[ChecklistItem]:
    return build_items(
        DEFAULT_REQUIREMENTS,
        supplier,
        lines if lines is not None else [AEC_LINE, PLAIN_LINE],
        documents or [],
        attachments or [],
        TODAY,
    )


def by_code(items: list[ChecklistItem]) -> dict[str, ChecklistItem]:
    return {item.code: item for item in items}


def test_scopes_decide_which_items_apply() -> None:
    distributor = [i.code for i in items_for(DISTRIBUTOR)]
    manufacturer = [i.code for i in items_for(MANUFACTURER)]

    # The AEC-Q report applies to the AEC-Q line only; PPAP to no line here.
    assert distributor == ["rohs", "reach_svhc", "cmrt", "aec_q", "iso_9001"]
    assert manufacturer == ["rohs", "reach_svhc", "cmrt", "aec_q", "iatf_16949", "iso_9001"]
    aec = by_code(items_for())["aec_q"]
    assert (aec.rfq_line_id, aec.line_position, aec.line_mpn) == (AEC_LINE.id, 1, AEC_LINE.mpn)
    assert all(i.status == "missing" for i in items_for())


def test_status_follows_the_latest_document_and_its_expiry() -> None:
    items = by_code(
        items_for(
            documents=[
                # Older expired RoHS replaced by a newer one: received.
                doc("rohs", expires=date(2025, 1, 1), recorded=date(2025, 1, 1)),
                doc("rohs", issued=date(2026, 6, 1)),
                # REACH issued 13 months ago, 12-month validity: expired.
                doc("reach_svhc", issued=date(2025, 9, 1)),
                # ISO 9001 with no expiry and no validity: received for good.
                doc("iso_9001"),
                # An AEC-Q report recorded without a line does not cover the line.
                doc("aec_q"),
            ]
        )
    )

    assert items["rohs"].status == "received"
    assert items["rohs"].document is not None
    assert items["rohs"].document.expires_on == date(2027, 6, 1)
    assert items["reach_svhc"].status == "expired"
    assert items["iso_9001"].status == "received" and items["iso_9001"].document is not None
    assert items["iso_9001"].document.expires_on is None
    assert items["aec_q"].status == "missing"
    assert items["cmrt"].status == "missing"


def test_line_documents_cover_their_line_only() -> None:
    items = by_code(items_for(documents=[doc("aec_q", line=AEC_LINE.id)]))

    assert items["aec_q"].status == "received"


def test_candidates_come_from_matching_filenames() -> None:
    other_rfq = uuid4()
    attachments = [
        ReceivedAttachment(uuid4(), "RoHS_Declaration_2026.pdf", RFQ, at(date(2026, 9, 2))),
        ReceivedAttachment(uuid4(), "AEC-Q200 report GCM188.pdf", RFQ, at(date(2026, 9, 3))),
        ReceivedAttachment(uuid4(), "aecq_other_part.pdf", other_rfq, at(date(2026, 9, 4))),
        ReceivedAttachment(uuid4(), "quote.xlsx", RFQ, at(date(2026, 9, 5))),
    ]
    items = by_code(items_for(attachments=attachments))

    assert [c.filename for c in items["rohs"].candidates] == ["RoHS_Declaration_2026.pdf"]
    # Per-line candidates come from the same RFQ only.
    assert [c.filename for c in items["aec_q"].candidates] == ["AEC-Q200 report GCM188.pdf"]
    assert items["cmrt"].candidates == []

    confirmed = by_code(
        items_for(attachments=attachments, documents=[doc("rohs", attachment=attachments[0].id)])
    )
    assert confirmed["rohs"].status == "received" and confirmed["rohs"].candidates == []


def test_summary_lists_blocking_items_not_received() -> None:
    items = items_for(documents=[doc("rohs")])
    summary = summarize(DISTRIBUTOR, items)

    assert (summary.counts.received, summary.counts.missing, summary.counts.expired) == (1, 4, 0)
    # RoHS is received; REACH and the line's AEC-Q report block; CMRT/ISO do not.
    assert [(b.code, b.rfq_line_id) for b in summary.blocking] == [
        ("reach_svhc", None),
        ("aec_q", AEC_LINE.id),
    ]


def test_custom_line_keyword_and_helpers() -> None:
    ppap = Requirement(code="ppap", name="PPAP", scope="line", line_keyword="ppap")
    line = LineInfo(id=uuid4(), rfq_id=RFQ, position=3, mpn=None, qualification="PPAP level 3")
    items = build_items([ppap], DISTRIBUTOR, [line, PLAIN_LINE], [], [], TODAY)

    assert [(i.code, i.rfq_line_id) for i in items] == [("ppap", line.id)]
    assert filename_matches("ISO-9001_cert.PDF", ["iso 9001"])
    assert not filename_matches("quote.pdf", ["rohs"])
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2026, 11, 15), 14) == date(2028, 1, 15)
