"""Golden expectations: what a correct import and analysis produce for a dataset.

The analysis numbers come from the production formulas (`purchasing.analysis`)
applied to the dataset itself, the way a perfect import would load it: a
supplier is a "distributor" unless the dataset gives its type, a received line
without a received quantity remains unknown, every line keeps
its currency. So a mismatch in an eval means the spreadsheet was read wrong, not
that the math changed.
"""

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from bido_back.purchasing import analysis as calc
from evals.data import Dataset


def _id(kind: str, key: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"bido-evals:{kind}:{key}")


def golden_analysis(
    dataset: Dataset, today: date, quantity: Decimal | None
) -> dict[str, Any] | None:
    """Expected lines for the dataset's main product; None if it cannot be analysed."""
    demands: list[calc.Demand] | None = None
    if quantity is None:
        planned = [p for p in dataset.plan if p.product == dataset.product and p.need_by >= today]
        if not planned:
            return None
        quantity = sum((p.quantity for p in planned), Decimal(0))
        need_by: date | None = min(p.need_by for p in planned)
        demands = [calc.Demand(p.quantity, p.need_by) for p in planned]
    else:
        need_by = None
    items = {
        p.number: calc.ItemInfo(
            id=_id("item", p.number),
            part_number=p.number,
            description=p.description,
            mpn=p.mpn,
            manufacturer=p.manufacturer,
            category=p.category,
            unit=p.unit,
        )
        for p in dataset.parts
    }
    bom = [calc.BomEntry(items[p.number], p.qty_per_unit) for p in dataset.parts]
    stock = {
        items[number].id: calc.Stock(
            on_hand=held[0], on_order=held[1], on_order_due=dataset.stock_due
        )
        for number, held in dataset.stock.items()
    }
    purchases = [
        calc.Purchase(
            item_id=items[row.part].id,
            supplier_id=_id("supplier", row.supplier.lower()),
            supplier_name=row.supplier,
            supplier_type=dataset.supplier(row.supplier).type,
            currency=row.currency,
            ordered_at=row.ordered_at,
            quantity=row.quantity,
            unit_price=row.unit_price,
            promised_at=row.promised_at,
            received_at=row.received_at,
            received_qty=row.received_qty,
            rejected_qty=row.rejected_qty,
        )
        for row in dataset.purchases
    ]
    result = calc.analyze(
        product=dataset.product,
        quantity=quantity,
        need_by=need_by,
        bom=bom,
        stock=stock,
        purchases=purchases,
        today=today,
        demands=demands,
    )
    return {
        "product": dataset.product,
        "quantity": str(quantity),
        "items_to_buy": result.summary.items_to_buy,
        "total_at_target": str(result.summary.total_at_target),
        "lines": [
            {
                "part_number": line.item.part_number,
                "description": line.item.description,
                "mpn": line.item.mpn,
                "to_buy": str(line.to_buy),
                "target_price": str(line.target_price) if line.target_price is not None else None,
                "max_price": str(line.max_price) if line.max_price is not None else None,
                "suggested": sorted(s.name for s in line.suggested),
                "spend_at_target": str(line.to_buy * line.target_price)
                if line.target_price is not None
                else "0",
            }
            for line in result.lines
        ],
    }


def golden_contacts(dataset: Dataset) -> list[list[str]]:
    """(supplier, email) pairs the import should create, sorted."""
    return sorted([s.name, s.email] for s in dataset.suppliers)
