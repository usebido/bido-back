"""Reading company data from spreadsheets: columns, values, row errors.

Deterministic, no model: a header is mapped to a field when its normalized
text (lower case, no accents, no punctuation) is one of the field's synonyms
(Portuguese, English and Italian). Every value is parsed and checked here;
a row with any bad value is reported and skipped, the rest is imported.

The five import types and their columns (* = required):

* `items`: part_number*, description*, mpn, manufacturer, category, unit
* `bom`: product*, part_number*, qty_per_unit*, description, mpn, manufacturer,
  category, unit (item columns create the item when it is new)
* `inventory`: part_number*, on_hand*, on_order, location, as_of
* `purchase_history`: po_number*, supplier*, part_number*, quantity*,
  unit_price*, ordered_at*, currency, promised_at, received_at, received_qty,
  rejected_qty, quality_ok, line_no, description
* `production_plan`: product*, quantity*, need_by*
* `supplier_contacts`: supplier*, email*, contact_name, phone, country, language

Workbooks with several sheets, headers below row 1 or mixed content go
through `bido_back.purchasing.workbook`, which finds the header row and the
columns of each section and then validates rows here (`parse_mapped`).
"""

import csv
import io
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Final, Literal, Protocol, cast

from bido_back.quotes.parsing import DecimalSeparator, number_tokens, parse_decimal

# What POST /v1/data/imports accepts (unchanged contract); workbooks add contacts.
TableImportType = Literal["items", "bom", "inventory", "purchase_history", "production_plan"]
ImportType = TableImportType | Literal["supplier_contacts"]
FieldKind = Literal["text", "number", "date", "bool", "int"]

MAX_ROWS: Final = 20_000
MAX_ERRORS: Final = 50
_EXCEL_EPOCH: Final = date(1899, 12, 30)


@dataclass(frozen=True, slots=True)
class FieldSpec:
    name: str
    kind: FieldKind
    required: bool
    synonyms: tuple[str, ...]
    # Matched before `synonyms` (the supplier's NAME over a generic "Supplier"
    # column that may hold its code).
    preferred: tuple[str, ...] = ()
    # Matched only after every field had its chance at its own synonyms: words
    # like "Item" or "Material" that name a field in one table and another field
    # in the next ("Item" is the line number next to a "Material" column).
    weak: tuple[str, ...] = ()


def _norm(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", folded.lower()).strip()


def norm(text: str) -> str:
    """Lower case, no accents, no punctuation: how headers are compared."""
    return _norm(text)


_PART = (
    "part number", "part no", "pn", "codigo", "codigo item", "codigo interno", "item code",
    "sku", "codice", "codice articolo", "articolo", "internal pn", "material",
    "material number", "numero material", "codigo material", "cod material",
    "codice materiale", "componente", "component", "codice componente",
)  # fmt: skip
_PART_WEAK = ("item", "cod", "code", "ref", "referencia", "riferimento")
_DESC = (
    "description", "descricao", "descrizione", "desc", "nome", "name", "item name",
    "item description", "texto breve", "short text", "texto do objeto", "txt brv material",
    "texto breve material", "material description", "descricao material",
    "descricao do material", "object description", "testo breve", "descrizione materiale",
)  # fmt: skip
_MPN = (
    "mpn", "manufacturer part number", "codigo fabricante", "part number fabricante",
    "codice produttore", "manufacturer pn", "n peca fabricante", "no peca fabricante",
    "numero peca fabricante", "peca fabricante", "manufacturer part no", "mfr part number",
    "mfr pn", "mfg part number", "codigo do fabricante",
)  # fmt: skip
_MANUFACTURER = ("manufacturer", "fabricante", "produttore", "marca", "brand", "maker")
_CATEGORY = ("category", "categoria", "familia", "family", "famiglia", "tipo", "type")
_UNIT = (
    "unit", "unidade", "un", "unita", "uom", "um", "unit of measure", "um basica",
    "unidade medida", "unidade de medida", "base unit", "base uom", "unita di misura",
)  # fmt: skip
_PRODUCT = ("product", "produto", "prodotto", "model", "modelo", "modello", "assembly")
_QTY = (
    "quantity", "quantidade", "qtd", "qty", "quantita", "qta", "qt", "qtd do pedido",
    "quantidade do pedido", "qtd pedido", "order qty", "order quantity", "ordered qty",
    "qty ordered", "quantita ordinata", "qta ordinata",
)  # fmt: skip
_SUPPLIER_NAME = (
    "supplier name", "nome do fornecedor", "nome fornecedor", "fornecedor nome", "vendor name",
    "razao social", "ragione sociale", "nome fornitore", "denominazione fornitore",
    "company name", "nome da empresa", "name of supplier",
)  # fmt: skip
_SUPPLIER_CODE = (
    "supplier code", "supplier id", "supplier no", "supplier number", "vendor code",
    "vendor id", "vendor no", "vendor number", "codigo fornecedor", "cod fornecedor",
    "codigo do fornecedor", "n fornecedor", "conta fornecedor", "codice fornitore",
    "cod fornitore", "lifnr",
)  # fmt: skip
_SUPPLIER_GENERIC = ("supplier", "fornecedor", "fornitore", "vendor")

SPECS: Final[Mapping[ImportType, tuple[FieldSpec, ...]]] = {
    "items": (
        FieldSpec("part_number", "text", True, _PART, weak=_PART_WEAK),
        FieldSpec("description", "text", True, _DESC),
        FieldSpec("mpn", "text", False, _MPN),
        FieldSpec("manufacturer", "text", False, _MANUFACTURER),
        FieldSpec("category", "text", False, _CATEGORY),
        FieldSpec("unit", "text", False, _UNIT),
    ),
    "bom": (
        FieldSpec(
            "product",
            "text",
            True,
            (
                *_PRODUCT,
                "parent",
                "parent item",
                "parent part",
                "item pai",
                "codigo pai",
                "produto acabado",
                "finished good",
                "finished product",
                "assieme",
                "conjunto",
            ),
            weak=("material",),
        ),  # fmt: skip
        FieldSpec("part_number", "text", True, _PART, weak=_PART_WEAK),
        FieldSpec(
            "qty_per_unit",
            "number",
            True,
            (
                "qty per unit",
                "quantity per unit",
                "quantidade por unidade",
                "qtd por unidade",
                "qtd unidade",
                "quantita per unita",
                "qta per unita",
                "qty per",
                "usage",
                "qtd componente",
                "quantidade componente",
                "component qty",
                "comp qty",
                "qta componente",
                *_QTY,
            ),
        ),  # fmt: skip
        FieldSpec("description", "text", False, _DESC),
        FieldSpec("mpn", "text", False, _MPN),
        FieldSpec("manufacturer", "text", False, _MANUFACTURER),
        FieldSpec("category", "text", False, _CATEGORY),
        FieldSpec("unit", "text", False, _UNIT),
        # A multi-level BOM ("0", ".1", "..2"): exploded to the parts to buy.
        FieldSpec(
            "level",
            "text",
            False,
            ("level", "nivel", "livello", "lvl", "bom level", "explosion level", "stufe"),
        ),
    ),
    "inventory": (
        FieldSpec("part_number", "text", True, _PART, weak=_PART_WEAK),
        FieldSpec(
            "on_hand",
            "number",
            True,
            (
                "on hand",
                "qty on hand",
                "estoque",
                "em estoque",
                "saldo",
                "saldo estoque",
                "giacenza",
                "disponibile",
                "stock",
                "in stock",
                # Unrestricted stock only: quality inspection and blocked stock
                # cannot go into a build, so they are not counted (see the module doc).
                "utilizacao livre",
                "livre utilizacao",
                "estoque livre",
                "estoque disponivel",
                "free stock",
                "unrestricted",
                "unrestricted use",
                "unrestricted stock",
                "available",
                "available stock",
                "qty available",
                "quantidade disponivel",
                "disponivel",
                "libero",
                "uso libero",
                "stock libero",
                "giacenza disponibile",
                "current stock",
                "estoque atual",
                "saldo atual",
            ),
        ),  # fmt: skip
        FieldSpec(
            "on_order",
            "number",
            False,
            (
                "on order",
                "em pedido",
                "pedido em aberto",
                "pedidos abertos",
                "ordinato",
                "in ordine",
                "open orders",
                "open po",
            ),
        ),  # fmt: skip
        FieldSpec(
            "location",
            "text",
            False,
            ("location", "local", "deposito", "magazzino", "warehouse", "storage location"),
        ),
        FieldSpec("as_of", "date", False, ("as of", "data inventario", "inventory date", "date")),
    ),
    "purchase_history": (
        FieldSpec(
            "po_number",
            "text",
            True,
            (
                "po",
                "po number",
                "po no",
                "pedido",
                "numero pedido",
                "numero do pedido",
                "n pedido",
                "no pedido",
                "n do pedido",
                "num pedido",
                "nr ordine",
                "n ordine",
                "ordine",
                "numero ordine",
                "order number",
                "purchase order",
                "doc compras",
                "documento de compras",
                "documento compras",
                "pedido de compra",
                "purchasing document",
                "purchase order number",
                "po doc",
                "ordine d acquisto",
                "numero oda",
            ),
        ),  # fmt: skip
        FieldSpec("supplier", "text", True, _SUPPLIER_GENERIC, preferred=_SUPPLIER_NAME),
        # The supplier's code in the buyer's ERP, next to its name: used to match the
        # supplier master (contacts) that may list suppliers by code only.
        FieldSpec("supplier_code", "text", False, _SUPPLIER_CODE, weak=_SUPPLIER_GENERIC),
        FieldSpec("part_number", "text", True, _PART, weak=_PART_WEAK),
        FieldSpec("quantity", "number", True, _QTY),
        FieldSpec(
            "unit_price",
            "number",
            True,
            (
                "unit price",
                "preco unitario",
                "prezzo unitario",
                "price",
                "preco",
                "prezzo",
                "valor unitario",
                "preco liquido",
                "net price",
                "prezzo netto",
                "unit cost",
                "custo unitario",
                "costo unitario",
            ),
        ),  # fmt: skip
        # SAP prices are per a price unit ("Por" 100): the unit price is price / unit.
        FieldSpec(
            "price_unit",
            "number",
            False,
            ("por", "price unit", "per", "unidade de preco", "unita di prezzo", "price per"),
        ),
        FieldSpec(
            "ordered_at",
            "date",
            True,
            (
                "order date",
                "ordered at",
                "ordered",
                "data pedido",
                "data do pedido",
                "data ordine",
                "po date",
                "data doc",
                "data documento",
                "data do documento",
                "document date",
                "data emissao",
                "data de emissao",
            ),
        ),  # fmt: skip
        FieldSpec("currency", "text", False, ("currency", "moeda", "valuta", "ccy")),
        FieldSpec(
            "promised_at",
            "date",
            False,
            (
                "promised",
                "promised date",
                "promised at",
                "data prometida",
                "data prevista",
                "data consegna prevista",
                "due date",
                "confirmed date",
                "data remessa",
                "data de remessa",
                "data entrega prevista",
                "data de entrega prevista",
                "scheduled delivery",
            ),
        ),  # fmt: skip
        FieldSpec(
            "received_at",
            "date",
            False,
            (
                "received",
                "received date",
                "received at",
                "data recebimento",
                "data entrega",
                "data consegna",
                "data ricevimento",
                "delivery date",
                "data em",
                "data entrada",
                "data de entrada",
                "data entrada mercadoria",
                "data do recebimento",
                "gr date",
                "goods receipt date",
                "data entrata merci",
            ),
        ),  # fmt: skip
        FieldSpec(
            "received_qty",
            "number",
            False,
            (
                "received qty",
                "qty received",
                "quantidade recebida",
                "qtd recebida",
                "qta ricevuta",
                "quantita ricevuta",
                "qtd fornecida",
                "quantidade fornecida",
                "qtd entregue",
                "quantidade entregue",
                "delivered qty",
                "qty delivered",
                "delivered quantity",
                "qta consegnata",
                "quantita consegnata",
            ),
        ),  # fmt: skip
        FieldSpec(
            "rejected_qty",
            "number",
            False,
            (
                "rejected",
                "rejected qty",
                "rejeitado",
                "quantidade rejeitada",
                "qtd rejeitada",
                "scarti",
                "qta scartata",
                "scartati",
                "qtd devolvida",
                "quantidade devolvida",
                "devolvido",
                "devolvida",
                "returned",
                "returned qty",
                "qty returned",
                "qta resa",
            ),
        ),  # fmt: skip
        FieldSpec(
            "quality_ok",
            "bool",
            False,
            ("quality ok", "qualidade ok", "chegou ok", "conforme", "ok", "qualita ok", "quality"),
        ),  # fmt: skip
        FieldSpec(
            "line_no",
            "int",
            False,
            ("line", "line no", "linha", "riga", "item no", "po item", "po line", "item do pedido"),
            weak=("item", "pos", "posicao", "position"),
        ),
        FieldSpec("description", "text", False, _DESC),
    ),
    "production_plan": (
        FieldSpec(
            "product",
            "text",
            True,
            _PRODUCT,
            # In a plan, the material planned IS the product.
            weak=(
                "material",
                "item",
                "codigo",
                "codigo material",
                "sku",
                "codice",
                "articolo",
                "codice articolo",
                "part number",
            ),
        ),  # fmt: skip
        FieldSpec(
            "quantity",
            "number",
            True,
            (
                *_QTY,
                "qtd planejada",
                "quantidade planejada",
                "planned qty",
                "planned quantity",
                "qta pianificata",
                "quantita pianificata",
                "qtd a produzir",
                "quantidade a produzir",
                "build qty",
                "quantity to build",
            ),
        ),  # fmt: skip
        FieldSpec(
            "need_by",
            "date",
            True,
            (
                "need by",
                "needed by",
                "data necessidade",
                "data",
                "date",
                "due date",
                "data necessaria",
                "consegna",
                "data produzione",
                "week",
                "periodo",
                "data de necessidade",
                "data requerida",
                "required date",
            ),
        ),  # fmt: skip
    ),
    "supplier_contacts": (
        FieldSpec(
            "supplier",
            "text",
            True,
            (*_SUPPLIER_GENERIC, "company", "empresa", "azienda"),
            preferred=_SUPPLIER_NAME,
        ),
        FieldSpec("supplier_code", "text", False, _SUPPLIER_CODE, weak=_SUPPLIER_GENERIC),
        FieldSpec(
            "email",
            "text",
            True,
            (
                "email",
                "e mail",
                "mail",
                "email contato",
                "email do contato",
                "contact email",
                "supplier email",
                "email fornecedor",
                "email fornitore",
                "indirizzo email",
            ),
        ),  # fmt: skip
        FieldSpec(
            "contact_name",
            "text",
            False,
            (
                "contact",
                "contact name",
                "contato",
                "nome contato",
                "referente",
                "nome referente",
                "pessoa de contato",
                "pessoa contato",
                "contact person",
                "persona di contatto",
                "persona contatto",
            ),
        ),  # fmt: skip
        FieldSpec("phone", "text", False, ("phone", "telefone", "telefono", "tel", "celular")),
        FieldSpec("country", "text", False, ("country", "pais", "paese", "nazione")),
        FieldSpec("language", "text", False, ("language", "idioma", "lingua", "lang")),
        # Manufacturer, authorized distributor, reseller...: brokers rank last.
        FieldSpec(
            "type",
            "text",
            False,
            (
                "supplier type",
                "tipo fornecedor",
                "tipo de fornecedor",
                "tipo fornitore",
                "classificacao",
                "classification",
                "categoria fornecedor",
                "supplier category",
                "tipologia",
                "tipologia fornitore",
                "type",
            ),
            weak=("tipo", "categoria"),
        ),  # fmt: skip
    ),
}

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_TRAILING_MINUS = re.compile(r"^(.*\d)\s*-$")
# Rows an ERP or a person adds under a table: not data, skipped without an error.
_TOTAL_WORDS = frozenset(
    {
        "total", "totais", "totale", "totali", "subtotal", "sub total", "subtotale",
        "grand total", "total geral", "totale generale", "soma", "somatorio", "summe",
        "gesamt", "sum",
    }
)  # fmt: skip
_TOTAL_MARKS = frozenset({"*", "**", "***"})  # SAP list subtotal markers

_TRUE = frozenset({"yes", "y", "sim", "s", "si", "true", "1", "ok", "x", "conforme", "vero"})
_FALSE = frozenset({"no", "n", "nao", "false", "0", "ko", "nok", "nc", "falso", "non conforme"})
_DATE_FORMATS: Final = (
    "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y", "%d/%m/%y", "%Y/%m/%d", "%d.%m.%y",
)  # fmt: skip


class ImportFileError(Exception):
    """The file cannot be read as a table (bad format, empty, too many rows)."""


@dataclass(frozen=True, slots=True)
class RowError:
    row: int  # 1-based spreadsheet row (the header is row 1)
    field: str | None
    message: str


@dataclass(slots=True)
class ParsedTable:
    import_type: ImportType
    columns: dict[str, str]  # field -> header as written
    missing_required: list[str]
    unknown_columns: list[str]
    rows: list[tuple[int, dict[str, object]]] = field(
        default_factory=list[tuple[int, dict[str, object]]]
    )
    errors: list[RowError] = field(default_factory=list[RowError])
    # Data rows read (skipped ones excluded): imported or failed.
    total_rows: int = 0
    # Totals and subtotals; for a multi-level BOM also the assemblies, exploded.
    skipped_rows: int = 0


# --- Reading the file ---------------------------------------------------------------------


def read_table(data: bytes, filename: str, sheet: str | None = None) -> list[list[object]]:
    """The first sheet (or `sheet`) of an xlsx, or a csv, as rows of raw cells."""
    lower = filename.lower()
    if lower.endswith(".xlsx"):
        return _read_xlsx(data, sheet)
    if lower.endswith(".csv"):
        return _read_csv(data)
    raise ImportFileError("imports accept .xlsx and .csv files")


class _Sheet(Protocol):
    @property
    def title(self) -> str: ...

    def iter_rows(self, *, values_only: bool) -> Iterable[tuple[object, ...]]: ...


class _Workbook(Protocol):
    @property
    def worksheets(self) -> list[_Sheet]: ...

    def close(self) -> None: ...


def _read_xlsx(data: bytes, sheet: str | None) -> list[list[object]]:
    # openpyxl ships no type information; contain it here.
    from openpyxl import load_workbook  # pyright: ignore[reportMissingTypeStubs]

    try:
        workbook = cast(
            _Workbook,
            load_workbook(io.BytesIO(data), read_only=True, data_only=True),
        )
    except Exception as exc:  # openpyxl raises several unrelated types on bad input
        raise ImportFileError("the file is not a readable .xlsx workbook") from exc
    try:
        sheets = workbook.worksheets
        if sheet is not None:
            sheets = [s for s in sheets if s.title == sheet]
            if not sheets:
                raise ImportFileError(f"no sheet named {sheet!r}")
        if not sheets:
            raise ImportFileError("the workbook has no sheet")
        rows: list[list[object]] = []
        for raw in sheets[0].iter_rows(values_only=True):
            rows.append(list(raw))
            if len(rows) > MAX_ROWS + 1:
                raise ImportFileError(f"imports are limited to {MAX_ROWS} rows")
        return rows
    finally:
        workbook.close()


def _read_csv(data: bytes) -> list[list[object]]:
    text = data.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ";" if text.count(";") > text.count(",") else ","
    rows: list[list[object]] = [list(r) for r in csv.reader(io.StringIO(text), delimiter=delimiter)]
    if len(rows) > MAX_ROWS + 1:
        raise ImportFileError(f"imports are limited to {MAX_ROWS} rows")
    return rows


# --- Mapping and values ---------------------------------------------------------------------


def _words(words: Iterable[str]) -> tuple[frozenset[str], frozenset[str]]:
    wanted = frozenset(_norm(w) for w in words)
    # "E-mail" and "Email", "Nº Pedido" and "N Pedido": compare without spaces too.
    return wanted, frozenset(w.replace(" ", "") for w in wanted)


def _find(normalized: Sequence[str], words: Iterable[str], used: set[int]) -> int | None:
    wanted, compact = _words(words)
    if not wanted:
        return None
    for position, name in enumerate(normalized):
        if position in used or not name:
            continue
        if name in wanted or name.replace(" ", "") in compact:
            return position
    return None


# A bare "Name" column is the SUPPLIER's name in a supplier list that has a
# separate contact-person column ("Fornecedor | Nome | Pessoa de contato").
_BARE_NAME: Final = ("name", "nome", "nombre", "denominacao", "denominazione")


def map_columns(
    import_type: ImportType, header: Sequence[object]
) -> tuple[dict[str, int], dict[str, str], list[str], list[str]]:
    """Field -> column index, field -> header text, missing required, unknown headers.

    Each field takes the first column matching its preferred names, else its
    synonyms; fields still unmatched then try their weak synonyms, so a weak
    word never takes a column another field names outright.
    """
    normalized = [_norm(str(cell)) if cell is not None else "" for cell in header]
    used: set[int] = set()
    index: dict[str, int] = {}
    specs = SPECS[import_type]
    for spec in specs:
        for words in (spec.preferred, (spec.name.replace("_", " "), *spec.synonyms)):
            position = _find(normalized, words, used)
            if position is not None:
                index[spec.name] = position
                used.add(position)
                break
    for spec in specs:
        if spec.name not in index:
            position = _find(normalized, spec.weak, used)
            if position is not None:
                index[spec.name] = position
                used.add(position)
    if (
        import_type == "supplier_contacts"
        and "supplier" in index
        and "supplier_code" not in index
        and "contact_name" in index
    ):
        named = _find(normalized, _BARE_NAME, used)
        if named is not None:
            index["supplier_code"] = index["supplier"]
            index["supplier"] = named
            used.add(named)
    columns = {name: str(header[i]) for name, i in index.items()}
    missing = [s.name for s in specs if s.required and s.name not in index]
    unknown = [
        str(cell) for i, cell in enumerate(header) if i not in used and cell not in (None, "")
    ]
    return index, columns, missing, unknown


def parse_number(value: object, decimal: DecimalSeparator | None) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    if isinstance(value, Decimal):
        return value
    text = str(value).strip()
    trailing = _TRAILING_MINUS.fullmatch(text)
    if trailing is not None:
        # SAP writes negatives with the sign after the number: "1.250,00-".
        number = parse_decimal(trailing.group(1), decimal=decimal)
        return None if number is None else -number
    return parse_decimal(text, decimal=decimal)


def decimal_evidence(cells: Iterable[object]) -> DecimalSeparator | None:
    """The decimal separator the numbers written as text in `cells` agree on.

    Only a number with a single reading votes: "1.234,56", "12,5" and "0,000"
    for the comma, "1,234.56" and "12.5" for the point. "1,000" or "888,000"
    (SAP's three-decimal quantities) read both ways and do not vote. No vote, or
    votes for both, gives None. Callers resolve the ambiguous numbers of a column
    with the evidence of that column first, then of its section, then of the
    whole workbook; never from one cell alone.
    """
    votes: set[DecimalSeparator] = set()
    for cell in cells:
        if not isinstance(cell, str):
            continue
        tokens = number_tokens(cell, with_pieces=False)
        if len(tokens) == 1 and len(tokens[0]) == 1 and tokens[0][0].decimal is not None:
            votes.add(tokens[0][0].decimal)
    return next(iter(votes)) if len(votes) == 1 else None


def parse_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, int | float) and not isinstance(value, bool) and 20000 < value < 80000:
        return _EXCEL_EPOCH + timedelta(days=int(value))  # an Excel serial date
    text = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    text = _norm(str(value))
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


def _blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def parse_rows(
    import_type: ImportType,
    raw_rows: Sequence[Sequence[object]],
    *,
    decimal: DecimalSeparator | None = None,
) -> ParsedTable:
    """Map the header row (the first non-empty row) and validate every data row."""
    header_at = next((i for i, row in enumerate(raw_rows) if any(not _blank(c) for c in row)), None)
    if header_at is None:
        raise ImportFileError("the file has no header row")
    header = raw_rows[header_at]
    index, _, _, _ = map_columns(import_type, header)
    data = [(n, row) for n, row in enumerate(raw_rows[header_at + 1 :], start=header_at + 2)]
    return parse_mapped(import_type, header, index, data, decimal=decimal)


def parse_mapped(
    import_type: ImportType,
    header: Sequence[object],
    index: Mapping[str, int],
    rows: Iterable[tuple[int, Sequence[object]]],
    *,
    decimal: DecimalSeparator | None = None,
) -> ParsedTable:
    """Validate data rows given an explicit field -> column index (any header row).

    `rows` are (1-based spreadsheet row number, cells). Unknown fields are refused.
    """
    specs = {s.name: s for s in SPECS[import_type]}
    unknown_fields = set(index) - set(specs)
    if unknown_fields:
        raise ImportFileError(f"unknown fields for {import_type}: {sorted(unknown_fields)}")
    width = len(header)
    columns = {
        name: (str(header[i]) if i < width and header[i] is not None else f"column {i + 1}")
        for name, i in index.items()
    }
    used = set(index.values())
    missing = [s.name for s in SPECS[import_type] if s.required and s.name not in index]
    unknown = [
        str(cell) for i, cell in enumerate(header) if i not in used and cell not in (None, "")
    ]
    table = ParsedTable(import_type, columns, missing, unknown)
    if missing:
        return table
    data = [(offset, raw) for offset, raw in rows if not all(_blank(c) for c in raw)]
    decimals: dict[str, DecimalSeparator | None] = {
        name: decimal_evidence(raw[i] for _, raw in data if i < len(raw)) or decimal
        for name, i in index.items()
        if specs[name].kind in ("number", "int")
    }
    for offset, raw in data:
        if _is_total_row(raw):
            table.skipped_rows += 1
            continue
        table.total_rows += 1
        if table.total_rows > MAX_ROWS:
            raise ImportFileError(f"imports are limited to {MAX_ROWS} rows")
        values: dict[str, object] = {}
        row_errors: list[RowError] = []
        for name, position in index.items():
            cell = raw[position] if position < len(raw) else None
            spec = specs[name]
            if _blank(cell):
                if spec.required:
                    row_errors.append(RowError(offset, name, "required value is empty"))
                continue
            parsed = _parse(spec.kind, cell, decimals.get(name, decimal))
            if parsed is None:
                row_errors.append(RowError(offset, name, f"not a valid {spec.kind}: {cell!r}"))
                continue
            values[name] = parsed
        row_errors.extend(_check_row(import_type, offset, values))
        if row_errors:
            if len(table.errors) < MAX_ERRORS:
                table.errors.extend(row_errors[: MAX_ERRORS - len(table.errors)])
            continue
        per = values.pop("price_unit", None)
        price = values.get("unit_price")
        if isinstance(per, Decimal) and isinstance(price, Decimal) and per != 1:
            values["unit_price"] = price / per
        table.rows.append((offset, values))
    return table


def _is_total_row(raw: Sequence[object]) -> bool:
    texts = [c.strip() for c in raw if isinstance(c, str) and c.strip()]
    return any(t in _TOTAL_MARKS or _norm(t) in _TOTAL_WORDS for t in texts)


def _parse(kind: FieldKind, cell: object, decimal: DecimalSeparator | None) -> object | None:
    match kind:
        case "text":
            text = str(cell).strip()
            if isinstance(cell, float) and cell.is_integer():
                text = str(int(cell))  # a numeric code read from Excel ("1020.0")
            return text or None
        case "number":
            return parse_number(cell, decimal)
        case "int":
            number = parse_number(cell, decimal)
            return int(number) if number is not None and number == int(number) else None
        case "date":
            return parse_date(cell)
        case "bool":
            return parse_bool(cell)


def _check_row(import_type: ImportType, row: int, values: Mapping[str, object]) -> list[RowError]:
    errors: list[RowError] = []

    def positive(name: str, *, allow_zero: bool = False) -> None:
        value = values.get(name)
        if isinstance(value, Decimal) and (value < 0 or (value == 0 and not allow_zero)):
            errors.append(RowError(row, name, "must be positive"))

    match import_type:
        case "bom":
            positive("qty_per_unit")
        case "inventory":
            positive("on_hand", allow_zero=True)
            positive("on_order", allow_zero=True)
        case "purchase_history":
            positive("quantity")
            positive("unit_price", allow_zero=True)
            positive("price_unit")
            positive("received_qty", allow_zero=True)
            positive("rejected_qty", allow_zero=True)
            currency = values.get("currency")
            if isinstance(currency, str) and not re.fullmatch(r"[A-Za-z]{3}", currency):
                errors.append(RowError(row, "currency", "use a 3-letter code such as EUR"))
        case "production_plan":
            positive("quantity")
        case "supplier_contacts":
            email = values.get("email")
            if isinstance(email, str) and not _EMAIL.fullmatch(email):
                errors.append(RowError(row, "email", "not an email address"))
            language = values.get("language")
            if isinstance(language, str) and not re.fullmatch(r"[A-Za-z]{2}", language):
                errors.append(RowError(row, "language", "use a 2-letter code such as it"))
        case "items":
            pass
    return errors


# What buyers' supplier masters call each supplier type (`suppliers.type`).
_SUPPLIER_TYPES: Final[dict[str, str]] = {
    **dict.fromkeys(
        ("manufacturer", "fabricante", "produttore", "fabbricante", "oem", "hersteller",
         "factory", "fabrica", "fabricant"),
        "manufacturer",
    ),
    **dict.fromkeys(
        ("authorized distributor", "authorised distributor", "distribuidor autorizado",
         "distributore autorizzato", "franchised distributor", "distribuidor oficial"),
        "authorized_distributor",
    ),
    **dict.fromkeys(
        ("distributor", "distribuidor", "distributore", "wholesaler", "atacadista",
         "distribuidora", "grossista"),
        "distributor",
    ),
    **dict.fromkeys(
        ("reseller", "revendedor", "revenda", "broker", "trader", "rivenditore",
         "intermediario", "independent distributor", "trading"),
        "reseller",
    ),
    **dict.fromkeys(
        ("machining", "usinagem", "machine shop", "lavorazioni meccaniche", "officina"),
        "machining",
    ),
}  # fmt: skip


def supplier_type(text: object) -> str | None:
    """A `suppliers.type` for what the buyer wrote ("Distribuidor autorizado"), or None."""
    return _SUPPLIER_TYPES.get(_norm(str(text))) if text is not None else None
