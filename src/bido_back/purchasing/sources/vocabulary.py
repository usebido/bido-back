"""Closed canonical vocabularies for categorical source fields.

The source mapper may interpret each distinct spelling of a categorical column as one
term of these vocabularies (`TableProposal.value_maps`); code checks the spelling and the
term before using it. Without such an interpretation, the built-in multilingual synonyms
below (Portuguese, English, Spanish, Italian, German, French) are the deterministic
fallback. Spellings that normalize to the same text but mean different things are left
out on purpose: French "ferme" (a firm, still open order) and "fermé" (closed) both
normalize to "ferme", and French "livré" (delivered) normalizes to Portuguese "livre"
(free). Such spellings stay unresolved unless the mapper interprets them in context.
"""

from collections.abc import Mapping
from typing import Final

from bido_back.purchasing.importers import norm

UNIT_VOCABULARY: Final = "unit"

# fmt: off
_CANCELLED: Final = (
    "cancelled", "canceled", "cancellation", "cancelado", "cancelada", "anulado", "anulada",
    "deleted", "excluido", "excluida", "eliminado", "eliminada", "void", "voided", "cancellato",
    "cancellata", "annullato", "annullata", "eliminato", "eliminata", "storniert", "annulliert",
    "geloscht", "annulé", "annulée", "supprimé", "supprimée",
)

# Vocabulary key -> canonical term -> source spellings (compared after `norm`).
_SYNONYMS: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "purchase.status": {
        "open": (
            "open", "opened", "em aberto", "aberto", "aberta", "parcial", "parcialmente recebido",
            "parcialmente recebida", "partially received", "partial", "partially delivered",
            "released", "liberado", "liberada", "confirmed", "confirmado", "confirmada", "firme",
            "abierto", "abierta", "parcialmente recibido", "parcialmente recibida", "aperto",
            "aperta", "parzialmente evaso", "parzialmente ricevuto", "confermato", "confermata",
            "offen", "teilgeliefert", "teilweise geliefert", "freigegeben", "bestätigt",
            "ouvert", "ouverte", "partiellement reçu", "partiellement livré", "confirmé",
            "confirmée",
        ),
        "closed": (
            "closed", "encerrado", "encerrada", "fechado", "fechada", "concluído", "concluída",
            "finalizado", "finalizada", "recebido", "recebida", "totalmente recebido",
            "received", "fully received", "completed", "complete", "done", "delivered",
            "fully delivered", "entregue", "cerrado", "cerrada", "completado", "completada",
            "recibido", "recibida", "entregado", "entregada", "terminado", "terminada",
            "chiuso", "chiusa", "evaso", "evasa", "completato", "completata", "ricevuto",
            "ricevuta", "consegnato", "consegnata", "erledigt", "abgeschlossen", "geschlossen",
            "geliefert", "vollständig geliefert", "clos", "close", "clôturé", "clôturée",
            "terminé", "terminée", "soldé", "soldée", "reçu", "reçue",
        ),
        "cancelled": _CANCELLED,
    },
    "delivery_schedule.status": {
        "pending": (
            "pending", "pendente", "not received", "não recebido", "não recebida", "em aberto",
            "aberto", "aberta", "open", "outstanding", "confirmed", "confirmado", "confirmada",
            "scheduled", "programado", "programada", "agendado", "agendada", "previsto",
            "prevista", "awaiting receipt", "aguardando", "aguardando recebimento",
            "em trânsito", "in transit", "pendiente", "no recibido", "no recibida", "abierto",
            "abierta", "en tránsito", "in attesa", "non ricevuto", "non ricevuta", "aperto",
            "aperta", "confermato", "confermata", "programmato", "programmata", "in transito",
            "offen", "ausstehend", "bestätigt", "nicht geliefert", "nicht erhalten", "unterwegs",
            "en attente", "non reçu", "non reçue", "confirmé", "confirmée", "ouvert", "ouverte",
            "en transit", "prévu", "prévue",
        ),
        "received": (
            "received", "recebido", "recebida", "delivered", "entregue", "recibido", "recibida",
            "entregado", "entregada", "ricevuto", "ricevuta", "consegnato", "consegnata",
            "geliefert", "erhalten", "eingegangen", "reçu", "reçue",
        ),
        "cancelled": _CANCELLED,
    },
    "production_plan.status": {
        "planned": (
            "planned", "firm planned", "planejada", "planejado", "firme", "planificada",
            "planificado", "planeada", "planeado", "pianificato", "pianificata", "geplant",
            "planifié", "planifiée",
        ),
        "released": (
            "released", "liberada", "liberado", "open", "em aberto", "aberta", "aberto",
            "lanzada", "lanzado", "abierta", "abierto", "rilasciato", "rilasciata", "lanciato",
            "lanciata", "aperto", "aperta", "freigegeben", "offen", "lancé", "lancée", "ouvert",
            "ouverte",
        ),
        "closed": (
            "closed", "encerrada", "encerrado", "fechada", "fechado", "concluída", "concluído",
            "finalizada", "finalizado", "completed", "complete", "done", "finished",
            "technically completed", "cerrada", "cerrado", "terminada", "terminado",
            "completada", "completado", "chiuso", "chiusa", "completato", "completata",
            "terminato", "terminata", "abgeschlossen", "erledigt", "beendet", "fertig",
            "clôturé", "clôturée", "terminé", "terminée", "clos", "close",
        ),
        "cancelled": _CANCELLED,
    },
    "supplier_item.status": {
        "approved": (
            "approved", "homologado", "homologada", "aprovado", "aprovada", "qualified",
            "qualificado", "qualificada", "active", "ativo", "ativa", "aprobado", "aprobada",
            "calificado", "calificada", "activo", "activa", "approvato", "approvata",
            "qualificato", "qualificata", "omologato", "omologata", "attivo", "attiva",
            "freigegeben", "zugelassen", "qualifiziert", "aktiv", "approuvé", "approuvée",
            "qualifié", "qualifiée", "homologué", "homologuée", "actif",
        ),
        "pending": (
            "pending", "pendente", "em homologação", "em avaliação", "under review",
            "in qualification", "pendiente", "en evaluación", "in attesa", "in valutazione",
            "in prüfung", "en attente",
        ),
        "blocked": (
            "blocked", "bloqueado", "bloqueada", "suspenso", "suspensa", "suspended", "inactive",
            "inativo", "inativa", "disqualified", "desqualificado", "desqualificada",
            "suspendido", "suspendida", "inactivo", "inactiva", "bloccato", "bloccata", "sospeso",
            "sospesa", "inattivo", "inattiva", "gesperrt", "inaktiv", "bloqué", "bloquée",
            "suspendu", "suspendue", "inactif",
        ),
    },
    "supplier.purchase_block": {
        # An ERP supplier master's purchasing block flag ("X"); blank is not blocked.
        "blocked": (
            "x", "sim", "s", "yes", "y", "true", "blocked", "bloqueado", "bloqueada",
            "bloqueio", "bloqueio de compras", "purchasing block", "purchase block", "gesperrt",
            "einkaufssperre", "bloccato", "bloccata", "bloqueo", "bloqué", "bloquée",
        ),
    },
    "material_requirement.element_type": {
        # MRP list elements (SAP MD04/MD05 abbreviations in pt, en, de, es).
        "requirement": (
            "requirement", "dependent requirement", "necdep", "necessidade dependente", "necind",
            "necpri", "necprim", "necessidade independente", "necessidade primaria", "reserv",
            "reserva", "reservation", "ordres", "depreq", "indreq", "cusord", "sales order",
            "ordven", "ordem de venda", "pedcli", "pedido de cliente", "sekbed", "primbd",
            "kdauft", "necesidad", "necesidad secundaria", "pedido cliente", "demanda",
            "demand", "consumo", "forecast req", "prev",
        ),
        "receipt": (
            "pedcpa", "pedcompra", "pedido de compra", "item de pedido", "poitem", "po item",
            "purchase order", "bs-pos", "bestpo", "bestellung", "ped. compra", "avrem",
            "aviso de remessa", "shpgnt", "shipping notification", "ordprd", "ordpro",
            "ordem de produção", "prdord", "production order", "fertauf",
        ),
        "proposal": (
            "reqcpa", "reqc", "solcom", "requisição de compra", "requisicao de compra",
            "purrqs", "purchase requisition", "banf", "ordpl", "ordpla", "ordplan",
            "ordem planejada", "ordem planej", "plord", "plord.", "planned order", "plauf",
            "solped", "orden planificada",
        ),
        "stock": (
            "est.cen", "estcen", "estoque centro", "estoque do centro", "plnt stock",
            "plant stock", "stock", "werkbest", "werksbestand", "estoque", "existencias",
        ),
        "safety_stock": (
            "estseg", "estoque de segurança", "estoque seguranca", "safest", "safety stock",
            "sichbe", "sicherheitsbestand", "stock de seguridad",
        ),
    },
    "material_requirement.sign_convention": {
        "negative": (
            "negative", "negativo", "negativa", "negatives", "negative requirements",
            "requirements are negative", "necessidades negativas",
        ),
        "positive": ("positive", "positivo", "positiva", "positives"),
    },
    "receipt.movement": {
        "receipt": (
            "101", "receipt", "goods receipt", "gr", "entrada", "entrada de mercadoria",
            "entrada de mercadorias", "recebimento", "entrada de mercancía", "recepción",
            "entrata", "entrata merce", "entrata merci", "ricevimento", "wareneingang", "we",
            "réception", "entrée", "entrée marchandise",
        ),
        "reversal": (
            "102", "reversal", "reverse", "estorno", "estornado", "estorno de entrada",
            "anulación", "storno", "stornierung", "annullamento", "annulation",
        ),
        "return": (
            "122", "161", "return", "return to vendor", "devolução", "devolução ao fornecedor",
            "devolución", "reso", "reso a fornitore", "rücksendung", "retour",
            "retour fournisseur",
        ),
    },
    "material_requirement.demand_basis": {
        "gross": (
            "gross", "gross demand", "gross requirement", "gross requirements", "bruta", "bruto",
            "demanda bruta", "necessidade bruta", "necessidades brutas", "necesidad bruta",
            "necesidades brutas", "lordo", "lorda", "fabbisogno lordo", "fabbisogni lordi",
            "brutto", "bruttobedarf", "brut", "besoin brut", "besoins bruts",
        ),
        "net": (
            "net", "net demand", "net requirement", "net requirements", "líquida", "líquido",
            "demanda líquida", "necessidade líquida", "necessidades líquidas", "neta", "neto",
            "necesidad neta", "necesidades netas", "netto", "netta", "fabbisogno netto",
            "fabbisogni netti", "nettobedarf", "besoin net", "besoins nets",
        ),
    },
    "inventory.balance_basis": {
        "available": (
            "available", "free", "unrestricted", "unrestricted use", "livre",
            "livre utilização", "disponível", "saldo livre", "estoque livre", "disponible",
            "libre", "libre utilización", "disponibile", "libero", "libero utilizzo", "frei",
            "frei verwendbar", "verfügbar", "libre utilisation",
        ),
        "gross": (
            "gross", "total", "physical", "physical stock", "físico", "físico bruto", "bruto",
            "bruta", "total físico", "estoque físico", "estoque total", "saldo total",
            "lordo", "giacenza totale", "brutto", "gesamt", "gesamtbestand", "physisch",
            "brut", "physique", "stock physique", "stock total",
        ),
    },
    UNIT_VOCABULARY: {
        # Individual pieces only: kits, sets, pairs, boxes, packs and rolls are not counts.
        "count": (
            "un", "und", "unid", "unids", "unidade", "unidades", "u", "ud", "uds", "unidad",
            "ea", "each", "pc", "pcs", "pce", "pces", "piece", "pieces", "peça", "peças", "pç",
            "pçs", "pz", "pzs", "pza", "pzas", "pezzo", "pezzi", "pieza", "piezas", "stk",
            "stck", "st", "stück", "stueck", "unit", "units", "unité", "unités", "nr",
        ),
        "kg": (
            "kg", "kgs", "kilo", "kilos", "quilo", "quilos", "quilograma", "quilogramas",
            "kilograma", "kilogramas", "kilogram", "kilograms", "kilogramm", "chilogrammo",
            "chilogrammi", "kilogramo", "kilogramos", "kilogramme", "kilogrammes",
        ),
        "g": (
            "g", "gr", "grs", "grama", "gramas", "gram", "grams", "gramm", "grammo", "grammi",
            "gramo", "gramos", "gramme", "grammes",
        ),
        "t": ("t", "tonelada", "toneladas", "tonne", "tonnes", "metric ton", "tonnellata"),
        "lb": ("lb", "lbs", "pound", "pounds", "libra", "libras"),
        "l": (
            "l", "lt", "lts", "ltr", "ltrs", "litro", "litros", "liter", "liters", "litre",
            "litres", "litri",
        ),
        "ml": (
            "ml", "mililitro", "mililitros", "milliliter", "milliliters", "millilitre",
            "millilitres", "millilitro", "millilitri",
        ),
        "m": (
            "m", "mt", "mts", "mtr", "mtrs", "metro", "metros", "meter", "meters", "metre",
            "metres", "metri",
        ),
        "cm": ("cm", "centímetro", "centímetros", "centimeter", "centimeters", "centimetri"),
        "mm": ("mm", "milímetro", "milímetros", "millimeter", "millimeters", "millimetri"),
        "m2": ("m2", "m²", "sqm", "metro quadrado", "metros quadrados", "square meter"),
        "m3": ("m3", "m³", "cbm", "metro cúbico", "metros cúbicos", "cubic meter"),
        "kit": ("kit", "kits"),
        "set": (
            "set", "sets", "jogo", "jogos", "jg", "conjunto", "conjuntos", "cj", "juego", "juegos",
            "satz",
        ),
        "pair": ("pair", "pairs", "par", "pares", "paar", "paio", "paia", "paire", "paires"),
        "box": (
            "box", "boxes", "cx", "caixa", "caixas", "caja", "cajas", "scatola", "scatole",
            "karton", "boîte", "boîtes",
        ),
        "pack": (
            "pack", "packs", "pk", "pkg", "package", "pacote", "pacotes", "pct", "paquete",
            "paquetes", "confezione", "confezioni", "packung", "paquet", "paquets",
        ),
        "roll": (
            "roll", "rolls", "rolo", "rolos", "rollo", "rollos", "rotolo", "rotoli", "rolle",
            "rollen", "rouleau", "rouleaux",
        ),
    },
}
# fmt: on


def _lookup(synonyms: Mapping[str, tuple[str, ...]]) -> dict[str, str]:
    found: dict[str, str] = {}
    for canonical, spellings in synonyms.items():
        for spelling in (canonical, *spellings):
            key = norm(spelling)
            if found.get(key, canonical) != canonical:
                raise ValueError(f"vocabulary spelling {spelling!r} has two meanings")
            found[key] = canonical
    return found


VOCABULARY: Final[dict[str, list[str]]] = {key: list(terms) for key, terms in _SYNONYMS.items()}
_BUILT_IN: Final[dict[str, dict[str, str]]] = {
    key: _lookup(terms) for key, terms in _SYNONYMS.items()
}


def vocabulary_key(kind: str, field: str) -> str | None:
    """The vocabulary a kind's categorical field is interpreted with, if any."""
    if field == "unit" or field.endswith("_unit"):
        return UNIT_VOCABULARY
    key = f"{kind}.{field}"
    return key if key in _SYNONYMS else None


def allowed(kind: str, field: str) -> list[str]:
    key = vocabulary_key(kind, field)
    return VOCABULARY[key] if key is not None else []


def built_in(kind: str, field: str, spelling: str | None) -> str | None:
    """The deterministic built-in meaning of a source spelling, or None when unknown."""
    key = vocabulary_key(kind, field)
    if key is None or spelling is None:
        return None
    return _BUILT_IN[key].get(norm(spelling))
