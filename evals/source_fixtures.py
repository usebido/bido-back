"""Synthetic scattered ERP evidence; no uploaded customer content is committed."""

from datetime import date
from decimal import Decimal

from bido_back.purchasing.sources.contracts import PlanningScope, Record, RecordKind, SourceRef

SCOPE = PlanningScope(
    product_code="MTR-100",
    location="Plant A",
    as_of=date(2026, 10, 8),
    horizon_end=date(2026, 10, 22),
    safety_stock=Decimal(0),
)
EXPECTED = {
    "000101": Decimal(50),
    "000102": Decimal(30),
    "000103": Decimal(0),
    "000104": Decimal(22),
    "000105": Decimal(220),
    "000106": Decimal(60),
}


def fact(kind: RecordKind, row: int = 2, **values: str) -> Record:
    return Record(
        kind=kind,
        values=values,
        evidence={
            field: SourceRef(source_id="synthetic-erp", table=kind, row=row, column=i)
            for i, field in enumerate(values, 1)
        },
    )


def motor_records() -> list[Record]:
    records = [
        fact("item", part_number="MTR-100", description="Motor", unit="pcs", area="Motors"),
        fact(
            "production_plan",
            reference="BUILD-1",
            product_code="MTR-100",
            quantity="40",
            unit="pcs",
            area="Motors",
            location="Plant A",
            need_by="2026-10-18",
            status="Planned",
        ),
        fact(
            "production_plan",
            3,
            reference="BUILD-2",
            product_code="MTR-100",
            quantity="30",
            unit="pcs",
            area="Motors",
            location="Plant A",
            need_by="2026-11-01",
            status="Planned",
        ),
        fact(
            "production_plan",
            4,
            reference="BUILD-B",
            product_code="MTR-100",
            quantity="100",
            unit="pcs",
            area="Motors",
            location="Plant B",
            need_by="2026-10-18",
            status="Planned",
        ),
        fact(
            "material_requirement",
            part_number="000303",
            quantity="35",
            unit="L",
            location="Plant A",
            area="Maintenance",
            need_by="2026-10-16",
            reference="REQ-M",
        ),
        fact("supplier", supplier_code="V1", name="Example Supplier"),
        fact("supplier", 3, supplier_code="V1", email="sales@supplier.example.test"),
    ]
    components = [
        ("000101", "Bearing", "pcs", "2", "25", "5", "0"),
        ("000102", "Shaft", "pcs", "1", "15", "5", "0"),
        ("000103", "Housing", "pcs", "1", "50", "0", "5"),
        ("000104", "Copper", "kg", "0.8", "12", "2", "0"),
        ("000105", "Magnet", "pcs", "8", "100", "20", "10"),
        ("000106", "Screw", "pcs", "4", "100", "0", "0"),
    ]
    for i, (part, description, unit, per, total, reserved, blocked) in enumerate(components, 7):
        records.extend(
            [
                fact(
                    "item", i, part_number=part, description=description, unit=unit, area="Motors"
                ),
                fact(
                    "bom",
                    i,
                    product_code="MTR-100",
                    part_number=part,
                    unit=unit,
                    quantity_per=per,
                    revision="A",
                    valid_from="2026-07-05",
                ),
                fact(
                    "inventory",
                    i,
                    part_number=part,
                    unit=unit,
                    location="Plant A",
                    total=total,
                    reserved=reserved,
                    blocked=blocked,
                    as_of="2026-10-08",
                ),
                fact(
                    "inventory",
                    i + 20,
                    part_number=part,
                    unit=unit,
                    location="Plant A",
                    total="999",
                    reserved="0",
                    blocked="0",
                    as_of="2026-09-19",
                ),
                fact(
                    "purchase",
                    i,
                    part_number=part,
                    unit=unit,
                    location="Plant A",
                    quantity="20",
                    unit_price="10.50",
                    currency="BRL",
                    ordered_at="2026-09-01",
                    status="Closed",
                    supplier_code="V1",
                    po_number=f"OLD-{i}",
                    po_line="00010",
                    promised_at="2026-09-10",
                ),
                fact(
                    "receipt",
                    i,
                    po_number=f"OLD-{i}",
                    po_line="00010",
                    location="Plant A",
                    quantity="20",
                    rejected="0",
                    unit=unit,
                    received_at="2026-09-10",
                    movement="101",
                    receipt_id=f"RECEIPT-{i}",
                ),
            ]
        )
    for i, (part, quantity, expected) in enumerate(
        [
            ("000101", "10", "2026-10-13"),
            ("000102", "20", "2026-10-26"),
            ("000105", "30", "2026-10-13"),
        ],
        40,
    ):
        records.extend(
            [
                fact(
                    "purchase",
                    i,
                    part_number=part,
                    quantity=quantity,
                    unit="pcs",
                    location="Plant A",
                    unit_price="12.00",
                    currency="BRL",
                    ordered_at="2026-10-05",
                    status="Open",
                    supplier_code="V1",
                    po_number=f"OPEN-{i}",
                    po_line="00010",
                    promised_at=expected,
                ),
                fact(
                    "delivery_schedule",
                    i,
                    part_number=part,
                    quantity=quantity,
                    unit="pcs",
                    location="Plant A",
                    expected_at=expected,
                    status="Not received",
                    po_number=f"OPEN-{i}",
                    po_line="00010",
                ),
            ]
        )
    return records
