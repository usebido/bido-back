"""SAP list reports saved to Excel or CSV, in Portuguese, English, German or Spanish.

- MB52 (warehouse stocks) -> inventory: unrestricted use is available; quality
  inspection, blocked and in-transit stock are kept apart.
- MD04 / MD05 (stock/requirements list) -> the stock line is inventory, the safety
  stock line an item's policy, requirements (signed negative) positive gross material
  requirements; purchase order items become open orders only when no order list is
  given; MRP proposals (requisitions, planned orders) are not counted.
- MM60 (material list) -> item master: MRP type, lot size, minimum lot, rounding value,
  safety stock, planned delivery time.
- ME1M (purchasing info records) -> supplier_item: price per price unit, validity,
  order unit and its conversion, lead time, minimum quantity; an expired record stays
  price history.
- ME2N / ME2M (purchase orders) -> purchase: the still-to-deliver quantity is the open
  balance. ME80FN (order history) -> purchase: goods-receipt quantity and date; fully
  received lines are closed.
- ME6H (vendor evaluation) -> supplier_performance scores.
- BP / XK03 (supplier master) -> supplier: name, e-mail, country, purchasing block.

Prices keep the report's price unit ("Unidade de preço" 1000) as
`price_basis_quantity`, and code divides. A report without a date (MB52 has none) is
dated by its title or file name, else by the MRP list's stock line of the same plant,
else today: each a disclosed assumption.
"""

import re
from collections import defaultdict
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from bido_back.purchasing.sources.contracts import (
    Assumption,
    CanonicalValue,
    EvidenceTable,
    Issue,
    Record,
    SourceRef,
)
from bido_back.purchasing.sources.evidence import join_key, source_date, source_number, source_text
from bido_back.purchasing.sources.formats import read_date
from bido_back.purchasing.sources.packs.base import (
    Column,
    PackContext,
    Reading,
    Recognition,
    ReportSpec,
    Row,
    data_rows,
    plain,
    put,
    recognize,
    record,
)
from bido_back.purchasing.sources.vocabulary import built_in

_MATERIAL = Column(
    "material",
    ("Material", "Material number", "Materialnummer", "Nº material", "Número do material"),
)
_DESCRIPTION = Column(
    "description",
    (
        "Texto breve de material",
        "Texto breve material",
        "Texto breve",
        "Material description",
        "Material Description",
        "Short text",
        "Short Text",
        "Materialkurztext",
        "Kurztext",
        "Descrição do material",
        "Texto breve de material/serviço",
    ),
)
_PLANT = Column("plant", ("Centro", "Plant", "Werk", "Planta"))
_SLOC = Column(
    "storage_location",
    ("Depósito", "Storage location", "Storage Location", "SLoc", "Lagerort", "Almacén"),
)
_BASE_UNIT = Column(
    "base_unit",
    (
        "UM básica",
        "UMB",
        "UM",
        "Base unit of measure",
        "Base Unit of Measure",
        "Base unit",
        "BUn",
        "Basismengeneinheit",
        "BME",
        "Unidade de medida básica",
    ),
)
_CURRENCY = Column("currency", ("Moeda", "Currency", "Crcy", "Währung", "Moneda"))
_VENDOR = Column(
    "vendor", ("Fornecedor", "Vendor", "Supplier", "Lieferant", "Proveedor", "Kreditor")
)
_VENDOR_NAME = Column(
    "vendor_name",
    (
        "Nome do fornecedor",
        "Vendor name",
        "Name of vendor",
        "Supplier name",
        "Name of supplier",
        "Name des Lieferanten",
        "Nombre del proveedor",
    ),
)
_NET_PRICE = Column(
    "net_price", ("Preço líquido", "Net price", "Net Price", "Nettopreis", "Precio neto")
)
_PRICE_UNIT = Column(
    "price_unit",
    (
        "Unidade de preço",
        "Price unit",
        "Price Unit",
        "Preiseinheit",
        "Por",
        "Per",
        "Unidad de precio",
    ),
)
_ORDER_UNIT = Column(
    "order_unit",
    (
        "Unidade de medida do pedido",
        "Unid.medida pedido",
        "Order unit",
        "Order Unit",
        "OUn",
        "Bestellmengeneinheit",
        "BME Bestellung",
        "Unidad medida pedido",
    ),
)
_PLANNED_DELIVERY = Column(
    "planned_delivery",
    (
        "Prazo entrega previsto",
        "Prazo entrega planejado",
        "Prazo de entrega planejado",
        "Planned delivery time",
        "Planned deliv. time",
        "Pl. deliv. time",
        "Planlieferzeit",
        "Plazo de entrega previsto",
    ),
)
_GROUP = Column(
    "material_group",
    (
        "Grupo de mercadorias",
        "Grupo mercadorias",
        "Material group",
        "Material Group",
        "Matl group",
        "Matl Group",
        "Warengruppe",
        "Grupo de artículos",
    ),
)
_SAFETY = Column(
    "safety_stock",
    (
        "Estoque de segurança",
        "Safety stock",
        "Safety Stock",
        "Sicherheitsbestand",
        "Stock de seguridad",
    ),
)

MB52 = ReportSpec(
    code="MB52",
    title="warehouse stocks",
    columns=(
        _MATERIAL,
        _DESCRIPTION,
        _PLANT,
        _SLOC,
        _BASE_UNIT,
        _CURRENCY,
        _GROUP,
        Column(
            "unrestricted",
            (
                "Utilização livre",
                "Utiliz.livre",
                "Unrestricted",
                "Unrestricted use",
                "Unrestr.-use stock",
                "Frei verwendbar",
                "Libre utilización",
            ),
        ),
        Column(
            "in_transit",
            (
                "Trânsito e transf.",
                "Em trânsito",
                "Transit and Transfer",
                "Transit and transfer",
                "In transit",
                "Transit/Transfer",
                "Transit und Umlag.",
                "En tránsito",
            ),
        ),
        Column(
            "quality",
            (
                "Em controle qualidade",
                "Controle de qualidade",
                "In Quality Insp.",
                "In quality inspection",
                "Quality inspection",
                "In Qualitätsprüf.",
                "Qualitätsprüfung",
                "En control de calidad",
            ),
        ),
        Column(
            "blocked",
            (
                "Bloqueado",
                "Estoque bloqueado",
                "Blocked",
                "Blocked stock",
                "Gesperrt",
                "Bloqueado stock",
            ),
        ),
    ),
    required=frozenset({"material", "plant", "unrestricted"}),
)
MD04 = ReportSpec(
    code="MD04",
    title="stock/requirements list",
    columns=(
        _MATERIAL,
        _DESCRIPTION,
        _PLANT,
        _BASE_UNIT,
        Column("date", ("Data", "Date", "Datum", "Fecha", "Data necessidade")),
        Column(
            "element",
            (
                "Elemento MRP",
                "MRP element",
                "MRP Element",
                "MRP elmnt",
                "Dispoelement",
                "Dispositionselement",
                "Elemento MRP/PMP",
            ),
        ),
        Column(
            "element_data",
            (
                "Dados do elemento MRP",
                "Dados elem.MRP",
                "MRP element data",
                "MRP element dt",
                "Daten zum Dispoelement",
                "Datos elemento MRP",
            ),
        ),
        Column(
            "reschedule",
            (
                "Data reprogramação",
                "Data de reprogramação",
                "Reschedule date",
                "Rescheduling date",
                "Umterminierungsdatum",
                "Fecha reprogramación",
            ),
        ),
        Column(
            "exception", ("Exceção", "Exception", "Ausnahme", "Mensagem de exceção", "Excepción")
        ),
        Column(
            "quantity",
            (
                "Entrada/necessidade",
                "Entrada/Necessidade",
                "Rec./reqd qty",
                "Rec./Reqd Qty",
                "Receipt/Requirement",
                "Receipt/reqmt qty",
                "Zugang/Bedarf",
                "Entrada/necesidad",
            ),
        ),
        Column(
            "available",
            (
                "Qtd.disponível",
                "Quantidade disponível",
                "Available qty",
                "Available quantity",
                "Avail. qty",
                "Verfügbare Menge",
                "Cantidad disponible",
            ),
        ),
    ),
    required=frozenset({"material", "element", "quantity", "date"}),
)
MM60 = ReportSpec(
    code="MM60",
    title="material list",
    columns=(
        _MATERIAL,
        _DESCRIPTION,
        _PLANT,
        _BASE_UNIT,
        _SAFETY,
        _PLANNED_DELIVERY,
        _GROUP,
        Column(
            "mrp_type",
            (
                "Tipo de MRP",
                "MRP type",
                "MRP Type",
                "Dispomerkmal",
                "Dispositionsmerkmal",
                "Característica de planificación",
            ),
        ),
        Column(
            "lot_size",
            (
                "Tamanho do lote",
                "Lot size",
                "Lot sizing procedure",
                "Losgröße",
                "Dispolosgröße",
                "Tamaño de lote",
            ),
        ),
        Column(
            "min_lot",
            (
                "Tamanho mínimo do lote",
                "Minimum lot size",
                "Min. lot size",
                "Mindestlosgröße",
                "Tamaño mínimo de lote",
            ),
        ),
        Column(
            "rounding",
            ("Valor de arredondamento", "Rounding value", "Rundungswert", "Valor de redondeo"),
        ),
    ),
    required=frozenset({"material", "mrp_type"}),
    any_of=frozenset({"safety_stock", "lot_size", "rounding", "planned_delivery"}),
)
ME1M = ReportSpec(
    code="ME1M",
    title="purchasing info records",
    columns=(
        _MATERIAL,
        _DESCRIPTION,
        _PLANT,
        _VENDOR,
        _VENDOR_NAME,
        _NET_PRICE,
        _CURRENCY,
        _PRICE_UNIT,
        _ORDER_UNIT,
        _PLANNED_DELIVERY,
        Column(
            "info_record",
            (
                "Registro info",
                "Registro de informação",
                "Info record",
                "Info Record",
                "Purchasing info rec.",
                "Infosatz",
                "Registro info compras",
            ),
        ),
        Column(
            "conversion",
            (
                "Conversão p/ UM básica",
                "Conversão para UM básica",
                "Conversion to base unit",
                "Conversion",
                "Umrechnung",
                "Conversión a UMB",
            ),
        ),
        Column(
            "minimum",
            (
                "Quantidade mínima",
                "Minimum quantity",
                "Minimum order qty",
                "Min. order qty",
                "Mindestmenge",
                "Cantidad mínima",
            ),
        ),
        Column(
            "valid_from",
            ("Validade de", "Válido de", "Valid from", "Valid From", "Gültig ab", "Válido desde"),
        ),
        Column(
            "valid_to",
            ("Validade até", "Válido até", "Valid to", "Valid To", "Gültig bis", "Válido hasta"),
        ),
    ),
    required=frozenset({"info_record", "material", "vendor"}),
)
PURCHASE_ORDERS = ReportSpec(
    code="ME2N",
    title="purchase orders",
    columns=(
        _MATERIAL,
        _DESCRIPTION,
        _PLANT,
        _CURRENCY,
        _VENDOR_NAME,
        _NET_PRICE,
        _PRICE_UNIT,
        _ORDER_UNIT,
        Column(
            "po",
            (
                "Documento de compra",
                "Documento compras",
                "Purchasing document",
                "Purchasing Document",
                "Purch.doc.",
                "Purchase order",
                "Einkaufsbeleg",
                "Documento de compras",
            ),
        ),
        Column("item", ("Item", "Item do documento de compra", "Position", "Pos.", "Posición")),
        Column(
            "document_date",
            ("Data do documento", "Document date", "Doc. date", "Belegdatum", "Fecha de documento"),
        ),
        # The vendor, or "code name" in one column (ME2N's vendor/supplying plant).
        Column("vendor", ("Fornecedor", "Vendor", "Supplier", "Lieferant", "Proveedor")),
        Column(
            "vendor_and_name",
            (
                "Fornecedor/centro fornecedor",
                "Supplier/Supplying Plant",
                "Vendor/supplying plant",
                "Lieferant/Lieferwerk",
                "Proveedor/centro suministrador",
            ),
        ),
        Column(
            "quantity",
            (
                "Qtd.do pedido",
                "Quantidade do pedido",
                "Order quantity",
                "Order Quantity",
                "PO quantity",
                "Bestellmenge",
                "Cantidad de pedido",
            ),
        ),
        Column(
            "delivery_date",
            (
                "Data de remessa",
                "Delivery date",
                "Delivery Date",
                "Deliv. date",
                "Lieferdatum",
                "Fecha de entrega",
            ),
        ),
        Column(
            "last_receipt",
            (
                "Data da última entrada de mercadorias",
                "Data última EM",
                "Last GR date",
                "Date of last GR",
                "Datum letzter WE",
            ),
        ),
        Column(
            "received",
            (
                "Qtd. entrada de mercadorias",
                "Qtd.entrada mercadorias",
                "GR quantity",
                "Goods receipt quantity",
                "Qty delivered",
                "WE-Menge",
                "Quantidade fornecida",
                "Cantidad entrada de mercancías",
            ),
        ),
        Column(
            "to_deliver",
            (
                "A fornecer (quantidade)",
                "Qtd. a fornecer",
                "Still to be delivered (qty)",
                "To be delivered (qty)",
                "Noch zu liefern (Menge)",
                "Por entregar (cantidad)",
            ),
        ),
    ),
    required=frozenset({"po", "item", "material", "quantity", "net_price"}),
)
ME6H = ReportSpec(
    code="ME6H",
    title="vendor evaluation",
    columns=(
        _VENDOR,
        _VENDOR_NAME,
        Column(
            "overall",
            ("Nota geral", "Overall score", "Total score", "Gesamtnote", "Puntuación global"),
        ),
        Column(
            "price", ("Nota Preço", "Nota preço", "Price score", "Preisnote", "Puntuación precio")
        ),
        Column(
            "quality",
            (
                "Nota Qualidade",
                "Nota qualidade",
                "Quality score",
                "Qualitätsnote",
                "Puntuación calidad",
            ),
        ),
        Column(
            "delivery",
            ("Nota Entrega", "Nota entrega", "Delivery score", "Liefernote", "Puntuación entrega"),
        ),
        Column(
            "service",
            ("Nota Serviço", "Nota serviço", "Service score", "Servicenote", "Puntuación servicio"),
        ),
        Column(
            "evaluated_at",
            (
                "Data da avaliação",
                "Evaluation date",
                "Date of evaluation",
                "Beurteilungsdatum",
                "Fecha de evaluación",
            ),
        ),
    ),
    required=frozenset({"vendor", "overall"}),
)
SUPPLIERS = ReportSpec(
    code="BP",
    title="supplier master",
    columns=(
        Column(
            "vendor",
            (
                "Fornecedor",
                "Vendor",
                "Supplier",
                "Parceiro de negócios",
                "Business partner",
                "Business Partner",
                "Lieferant",
                "Kreditor",
                "Proveedor",
            ),
        ),
        Column("name", ("Nome 1", "Name 1", "Nome", "Name", "Razão social", "Nombre 1")),
        Column("country", ("País", "Country", "Ctry", "Land", "País/região")),
        Column(
            "email",
            (
                "Endereço de e-mail",
                "E-mail",
                "E-Mail",
                "Email",
                "E-mail address",
                "E-Mail-Adresse",
                "Dirección de correo electrónico",
            ),
        ),
        Column(
            "purchase_block",
            (
                "Bloqueio de compras",
                "Bloqueio compras",
                "Purchasing block",
                "Purch. block",
                "Purchase block",
                "Einkaufssperre",
                "Bloqueo de compras",
            ),
        ),
    ),
    required=frozenset({"vendor", "name"}),
    any_of=frozenset({"country", "email", "purchase_block"}),
)
SPECS = (MB52, MD04, MM60, ME1M, PURCHASE_ORDERS, ME6H, SUPPLIERS)
# "4500019725/00010": an MRP element's order and item.
_ORDER_ITEM = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")
# "100104 USINAGEM PRECISA BRASIL LTDA": a code, then the name.
_CODE_NAME = re.compile(r"^\s*(\d+)\s+(\S.*?)\s*$")
# "18 L": how many base units one order unit holds.
_CONVERSION = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*([^\d\s].*?)\s*$")
_ISO_IN_NAME = re.compile(r"(20\d\d)[-_.]?([01]\d)[-_.]?([0-3]\d)")
_INFINITE = date(9999, 1, 1)


class SapPack:
    name = "sap"

    def recognize(self, table: EvidenceTable) -> Recognition | None:
        found = recognize(SPECS, table)
        if found is not None and found.spec is PURCHASE_ORDERS:
            columns = found.columns
            if "vendor" not in columns and "vendor_and_name" not in columns:
                return None
        return found

    def read(self, table: EvidenceTable, recognition: Recognition, context: PackContext) -> Reading:
        reading = Reading(table=table, recognition=recognition)
        spec = recognition.spec
        key = "vendor" if spec in (ME6H, SUPPLIERS) else "material"
        rows, skipped = data_rows(table, recognition, key)
        readers = {
            MB52: _mb52,
            MD04: _md04,
            MM60: _mm60,
            ME1M: _me1m,
            PURCHASE_ORDERS: _purchase_orders,
            ME6H: _me6h,
            SUPPLIERS: _suppliers,
        }
        for row in rows:
            readers[spec](row, reading)
            reading.covered.add(row.number)
        reading.covered.add(recognition.header_row)
        if spec is PURCHASE_ORDERS:
            # ME80FN (order history) and ME2N/ME2M (open orders) share one layout.
            columns = recognition.columns
            code = (
                "ME2N" if "to_deliver" in columns else "ME80FN" if "received" in columns else "ME2N"
            )
            reading.recognition = Recognition(
                spec=ReportSpec(
                    code=code,
                    title=spec.title,
                    columns=spec.columns,
                    required=spec.required,
                ),
                header_row=recognition.header_row,
                columns=columns,
            )
        if skipped:
            reading.issues.append(
                Issue(
                    code="report_rows_skipped",
                    severity="warning",
                    message=f"{table.name} ({reading.report}): {skipped} row(s) without a key "
                    "or with a subtotal mark were not read as transactions.",
                )
            )
        title_date = _title_date(table, recognition.header_row)
        named_date = _named_date(context.filenames.get(table.source_id, ""))
        if spec is MB52:
            for found in reading.records:
                if title_date is not None:
                    put(found, "as_of", title_date[0], title_date[1])
                elif named_date is not None:
                    put(found, "as_of", named_date, None)
            if title_date is None and named_date is not None:
                reading.assumptions.append(
                    Assumption(
                        code="stock_dated_by_file_name",
                        message=f"{table.name} (MB52) states no date; its stock is taken as of "
                        f"{named_date}, the date in the file name.",
                        alternative="If the export is from another day, stock movements since "
                        "then are not reflected.",
                    )
                )
        return reading

    def reconcile(
        self, readings: Sequence[Reading], context: PackContext
    ) -> tuple[list[Issue], list[Assumption]]:
        issues: list[Issue] = []
        assumptions: list[Assumption] = []
        by_report: dict[str, list[Reading]] = defaultdict(list)
        for reading in readings:
            by_report[reading.report].append(reading)
        mrp = [r for reading in by_report["MD04"] for r in reading.records]
        stock = [r for reading in by_report["MB52"] for r in reading.records]
        _date_stock(stock, mrp, context, assumptions)
        _one_stock(by_report["MD04"], stock, issues, assumptions)
        orders = [
            r for name in ("ME2N", "ME80FN") for reading in by_report[name] for r in reading.records
        ]
        _one_order_list(by_report["MD04"], orders, issues)
        _expired_info_records(by_report["ME1M"], mrp, stock, context, assumptions)
        return issues, assumptions


# --- Readers, one per report ------------------------------------------------------------


def _mb52(row: Row, reading: Reading) -> None:
    found = record(
        "inventory",
        row,
        {
            "part_number": "material",
            "description": "description",
            "location": "plant",
            "warehouse": "storage_location",
            "unit": "base_unit",
            "available": "unrestricted",
            "quality_inspection": "quality",
            "blocked": "blocked",
            "in_transit": "in_transit",
            "material_group": "material_group",
        },
    )
    reading.records.append(found)


def _md04(row: Row, reading: Reading) -> None:
    raw = row.text("element") or ""
    element = built_in("material_requirement", "element_type", raw)
    quantity = row.number_of("quantity")
    common = {
        "part_number": "material",
        "description": "description",
        "location": "plant",
        "unit": "base_unit",
    }
    if element == "stock":
        found = record("inventory", row, {**common, "as_of": "date", "available": "quantity"})
        found.values["balance_basis"] = raw
        found.evidence["balance_basis"] = row.ref("element")
        found.canonical["balance_basis"] = _term(raw, "available")
        reading.records.append(found)
    elif element == "safety_stock" and quantity is not None:
        found = record("item", row, {**common})
        put(
            found,
            "safety_stock",
            row.text("quantity") or "",
            row.ref("quantity"),
            plain(abs(quantity)),
        )
        reading.records.append(found)
    elif element == "requirement" and quantity is not None:
        found = record(
            "material_requirement",
            row,
            {**common, "need_by": "date", "reference": "element_data", "element_type": "element"},
        )
        found.canonical["element_type"] = _term(raw, "requirement")
        # Requirements are written as negative quantities: the demand is their size.
        put(
            found, "quantity", row.text("quantity") or "", row.ref("quantity"), plain(abs(quantity))
        )
        put(found, "demand_basis", raw, row.ref("element"), "gross")
        reading.records.append(found)
    elif element == "receipt" and quantity is not None:
        data = row.text("element_data") or ""
        order = _ORDER_ITEM.match(data)
        if order is None or not re.search(r"ped|po|bs|best|compra", raw, re.IGNORECASE):
            return  # A production order or shipping notification: not a purchase.
        found = record("purchase", row, {**common, "promised_at": "date"})
        put(
            found,
            "po_number",
            order.group(1),
            row.ref("element_data", order.start(1), order.end(1)),
        )
        put(found, "po_line", order.group(2), row.ref("element_data", order.start(2), order.end(2)))
        put(
            found, "quantity", row.text("quantity") or "", row.ref("quantity"), plain(abs(quantity))
        )
        put(
            found,
            "open_quantity",
            row.text("quantity") or "",
            row.ref("quantity"),
            plain(abs(quantity)),
        )
        put(found, "status", raw, row.ref("element"), "open")
        if row.text("exception") is not None and row.text("reschedule"):
            # The date MRP asks to bring the order forward to: an expedite request.
            put(found, "reschedule_to", row.text("reschedule") or "", row.ref("reschedule"))
        reading.records.append(found)
    if element == "receipt" and row.text("exception") is not None and row.text("reschedule"):
        data = row.text("element_data") or ""
        material = row.text("material") or ""
        reading.assumptions.append(
            Assumption(
                code="mrp_reschedule_in",
                message=f"The MRP list asks to bring {raw} {data} forward from "
                f"{row.text('date')} to {row.text('reschedule')} (exception "
                f"{row.text('exception')}); it is counted on its current date and listed "
                "as an expedite action.",
                alternative="Expediting it to the earlier date would avoid part of the "
                "purchase calculated before it arrives.",
                part_numbers=[material],
                evidence=[row.ref("element"), row.ref("reschedule")],
            )
        )
    elif element == "proposal":
        reading.assumptions.append(
            Assumption(
                code="mrp_proposal_not_counted",
                message=f"MRP proposal {raw} {row.text('element_data') or ''} was not counted: "
                "Bido calculates the purchase need itself.",
                part_numbers=[row.text("material") or ""],
                evidence=[row.ref("element")],
            )
        )
    elif element is None:
        reading.issues.append(
            Issue(
                code="unknown_mrp_element",
                severity="warning",
                message=f"{row.table.name}: MRP element {raw!r} is not recognized; the row "
                "was not used.",
                evidence=[row.ref("element")],
            )
        )


def _mm60(row: Row, reading: Reading) -> None:
    found = record(
        "item",
        row,
        {
            "part_number": "material",
            "description": "description",
            "unit": "base_unit",
            "location": "plant",
            "mrp_type": "mrp_type",
            "lot_size": "lot_size",
            "min_lot_size": "min_lot",
            "rounding": "rounding",
            "safety_stock": "safety_stock",
            "planned_delivery_days": "planned_delivery",
            "material_group": "material_group",
        },
    )
    reading.records.append(found)


def _me1m(row: Row, reading: Reading) -> None:
    link = record(
        "supplier_item",
        row,
        {
            "part_number": "material",
            "supplier_code": "vendor",
            "valid_from": "valid_from",
            "valid_to": "valid_to",
            "unit_price": "net_price",
            "currency": "currency",
            "unit": "order_unit",
            "lead_time_days": "planned_delivery",
            "min_order_quantity": "minimum",
        },
    )
    _price_unit(row, link)
    number = row.text("info_record")
    if number is not None:
        # A purchasing info record is SAP's source of supply for the material.
        put(link, "status", number, row.ref("info_record"), "approved")
    conversion = row.text("conversion")
    match = _CONVERSION.match(conversion) if conversion is not None else None
    if match is not None and link.values.get("unit"):
        put(link, "alt_unit", link.values["unit"], link.evidence.get("unit"))
        put(
            link,
            "alt_unit_factor",
            match.group(1),
            row.ref("conversion", match.start(1), match.end(1)),
        )
        item = Record(kind="item", values={}, evidence={})
        put(item, "part_number", link.values["part_number"], link.evidence["part_number"])
        put(item, "alt_unit", link.values["unit"], link.evidence.get("unit"))
        put(
            item,
            "alt_unit_factor",
            match.group(1),
            row.ref("conversion", match.start(1), match.end(1)),
        )
        reading.records.append(item)
    reading.records.append(link)
    if row.text("vendor") and row.text("vendor_name"):
        reading.records.append(
            record("supplier", row, {"supplier_code": "vendor", "name": "vendor_name"})
        )


def _price_unit(row: Row, target: Record) -> None:
    """SAP's price unit is a quantity ("1000": the price is per thousand)."""
    basis = row.number_of("price_unit")
    if basis is not None and basis > 0:
        put(
            target,
            "price_basis_quantity",
            row.text("price_unit") or "",
            row.ref("price_unit"),
            plain(basis),
        )


def _purchase_orders(row: Row, reading: Reading) -> None:
    found = record(
        "purchase",
        row,
        {
            "po_number": "po",
            "po_line": "item",
            "ordered_at": "document_date",
            "supplier_code": "vendor",
            "supplier_name": "vendor_name",
            "part_number": "material",
            "location": "plant",
            "quantity": "quantity",
            "unit": "order_unit",
            "unit_price": "net_price",
            "currency": "currency",
            "promised_at": "delivery_date",
            "received_at": "last_receipt",
            "received_quantity": "received",
            "open_quantity": "to_deliver",
        },
    )
    _price_unit(row, found)
    combined = row.text("vendor_and_name")
    match = _CODE_NAME.match(combined) if combined is not None else None
    if match is not None and "supplier_code" not in found.values:
        put(
            found,
            "supplier_code",
            match.group(1),
            row.ref("vendor_and_name", match.start(1), match.end(1)),
        )
        put(
            found,
            "supplier_name",
            match.group(2),
            row.ref("vendor_and_name", match.start(2), match.end(2)),
        )
    ordered = row.number_of("quantity")
    to_deliver = row.number_of("to_deliver")
    received = row.number_of("received")
    # The list's own balance says whether the line is still open.
    if to_deliver is not None:
        put(
            found,
            "status",
            row.text("to_deliver") or "",
            row.ref("to_deliver"),
            "open" if to_deliver > 0 else "closed",
        )
    elif received is not None and ordered is not None:
        put(
            found,
            "status",
            row.text("received") or "",
            row.ref("received"),
            "closed" if received >= ordered else "open",
        )
        if received < ordered:
            put(
                found,
                "open_quantity",
                row.text("quantity") or "",
                row.ref("quantity"),
                plain(ordered - received),
            )
    reading.records.append(found)


def _me6h(row: Row, reading: Reading) -> None:
    reading.records.append(
        record(
            "supplier_performance",
            row,
            {
                "supplier_code": "vendor",
                "name": "vendor_name",
                "score_overall": "overall",
                "score_price": "price",
                "score_quality": "quality",
                "score_delivery": "delivery",
                "score_service": "service",
                "as_of": "evaluated_at",
            },
        )
    )


def _suppliers(row: Row, reading: Reading) -> None:
    reading.records.append(
        record(
            "supplier",
            row,
            {
                "supplier_code": "vendor",
                "name": "name",
                "email": "email",
                "country": "country",
                "purchase_block": "purchase_block",
            },
        )
    )


# --- Across reports ---------------------------------------------------------------------


def _term(raw: str, canonical: str) -> CanonicalValue:
    return CanonicalValue(value=canonical, source=raw, basis="report_convention")


def _key(found: Record) -> tuple[str, str]:
    return (
        join_key(source_text(found, "part_number")) or "",
        join_key(source_text(found, "location")) or "",
    )


def _date_stock(
    stock: list[Record], mrp: list[Record], context: PackContext, assumptions: list[Assumption]
) -> None:
    """MB52 has no date: the MRP list's stock line of the same plant dates it, else today."""
    undated = [r for r in stock if "as_of" not in r.values]
    if not undated:
        return
    lines = [r for r in mrp if r.kind == "inventory"]
    dated: dict[str, tuple[str, SourceRef | None]] = {}
    for line in lines:
        plant = join_key(source_text(line, "location")) or ""
        if (value := source_text(line, "as_of")) is not None:
            dated.setdefault(plant, (value, line.evidence.get("as_of")))
    by_mrp: list[Record] = []
    by_today: list[Record] = []
    for found in undated:
        plant = join_key(source_text(found, "location")) or ""
        if plant in dated:
            put(found, "as_of", dated[plant][0], dated[plant][1])
            by_mrp.append(found)
        else:
            put(found, "as_of", context.today.isoformat(), None)
            by_today.append(found)
    if by_mrp:
        days = sorted({r.values["as_of"] for r in by_mrp})
        assumptions.append(
            Assumption(
                code="stock_dated_by_mrp_list",
                message="The warehouse stock list (MB52) states no date; its balances are taken "
                f"as of {', '.join(days)}, the date of the MRP list's (MD04) stock line.",
                alternative="If MB52 was exported on another day, stock movements in between "
                "are not reflected.",
            )
        )
    if by_today:
        assumptions.append(
            Assumption(
                code="stock_dated_today",
                message="The warehouse stock list (MB52) states no date and nothing else dates "
                f"it; its balances are taken as of today, {context.today.isoformat()}.",
                alternative="If the export is older, stock movements since then are not reflected.",
            )
        )


def _one_stock(
    mrp_readings: Sequence[Reading],
    stock: list[Record],
    issues: list[Issue],
    assumptions: list[Assumption],
) -> None:
    """MB52 per storage location wins over MD04's plant stock line; they must agree."""
    if not stock:
        return
    totals: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for found in stock:
        totals[_key(found)] += source_number(found, "available", []) or Decimal(0)
    differ: list[str] = []
    for reading in mrp_readings:
        kept: list[Record] = []
        for found in reading.records:
            if found.kind != "inventory" or _key(found) not in totals:
                kept.append(found)
                continue
            plant_stock = source_number(found, "available", [])
            if plant_stock is not None and plant_stock != totals[_key(found)]:
                differ.append(
                    f"{source_text(found, 'part_number')}: MB52 {plain(totals[_key(found)])}, "
                    f"MD04 {plain(plant_stock)}"
                )
        reading.records[:] = kept
    if differ:
        assumptions.append(
            Assumption(
                code="stock_lists_differ",
                message="The stock lists disagree; the warehouse list (MB52) was used: "
                + "; ".join(differ[:10])
                + ".",
                alternative="If the MRP list's (MD04) plant stock is right, the quantities to "
                "buy change by the difference.",
                confidence="low",
                part_numbers=[d.split(":", 1)[0] for d in differ],
            )
        )
        issues.append(
            Issue(
                code="stock_lists_differ",
                severity="warning",
                message=f"{len(differ)} material(s) have different stock in MB52 and MD04.",
            )
        )


def _one_order_list(
    mrp_readings: Sequence[Reading], orders: list[Record], issues: list[Issue]
) -> None:
    """With an order list, MD04's purchase order lines are the same orders: not twice.

    The date MD04 asks to move an order to is carried onto the listed order line.
    """
    if not orders:
        return
    listed: dict[tuple[str | None, str | None], Record] = {
        (join_key(source_text(r, "po_number")), join_key(source_text(r, "po_line"))): r
        for r in orders
    }
    missing = 0
    for reading in mrp_readings:
        kept: list[Record] = []
        for found in reading.records:
            if found.kind != "purchase":  # MD04's purchase records are its order lines.
                kept.append(found)
                continue
            key = (
                join_key(source_text(found, "po_number")),
                join_key(source_text(found, "po_line")),
            )
            order = listed.get(key)
            if order is None:
                missing += 1
            elif (moved := source_text(found, "reschedule_to")) is not None:
                put(order, "reschedule_to", moved, found.evidence.get("reschedule_to"))
        reading.records[:] = kept
    if missing:
        issues.append(
            Issue(
                code="mrp_order_not_listed",
                severity="warning",
                message=f"{missing} purchase order line(s) of the MRP list are not in the order "
                "list and were not counted.",
            )
        )


def _expired_info_records(
    readings: Sequence[Reading],
    mrp: list[Record],
    stock: list[Record],
    context: PackContext,
    assumptions: list[Assumption],
) -> None:
    """An info record past its validity is price history, not a source of supply."""
    days = [
        day
        for r in [*mrp, *stock]
        if r.kind == "inventory" and (day := source_date(r, "as_of", [])) is not None
    ]
    as_of = max(days, default=context.today)
    for reading in readings:
        for found in reading.records:
            if found.kind != "supplier_item":
                continue
            until = source_date(found, "valid_to", [])
            if until is None or until >= as_of or until >= _INFINITE:
                continue
            part = source_text(found, "part_number") or ""
            assumptions.append(
                Assumption(
                    code="info_record_expired",
                    message=f"The info record of {part} with supplier "
                    f"{source_text(found, 'supplier_code')} expired on {until.isoformat()}: "
                    "its price is history, not a valid agreement.",
                    part_numbers=[part],
                    evidence=[ref for ref in [found.evidence.get("valid_to")] if ref is not None],
                )
            )


def _title_date(table: EvidenceTable, header_row: int) -> tuple[str, SourceRef] | None:
    """A cell above the header (the report's title block) holding one unambiguous date."""
    for number in range(1, header_row):
        for column, cell in enumerate(table.rows[number - 1], start=1):
            if cell is not None and read_date(cell).value is not None:
                return cell.strip(), SourceRef(
                    source_id=table.source_id, table=table.name, row=number, column=column
                )
    return None


def _named_date(filename: str) -> str | None:
    match = _ISO_IN_NAME.search(filename)
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError:
        return None


SAP = SapPack()
