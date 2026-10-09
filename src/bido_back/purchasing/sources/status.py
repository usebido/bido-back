"""Supplier qualification statuses of the buyer's approved-source data."""

from typing import Final

from bido_back.purchasing.importers import norm
from bido_back.purchasing.sources.contracts import SupplierEvidence

# Compared after `norm`.
APPROVED: Final = frozenset(
    {"homologado", "homologada", "aprovado", "aprovada", "approved", "qualificado",
     "qualificada", "qualified", "certificado", "certificada", "certified", "preferencial",
     "preferred", "ativo", "ativa", "active"}
)  # fmt: skip
DISQUALIFIED: Final = frozenset(
    {"bloqueado", "bloqueada", "blocked", "suspenso", "suspensa", "suspended", "reprovado",
     "reprovada", "rejected", "desqualificado", "desqualificada", "disqualified",
     "descredenciado", "descredenciada", "inativo", "inativa", "inactive", "vencido",
     "vencida", "expired", "cancelado", "cancelada", "cancelled", "canceled", "debarred"}
)  # fmt: skip


def status(supplier: SupplierEvidence) -> str:
    return norm(supplier.qualification_status or "")


def disqualified(supplier: SupplierEvidence) -> bool:
    """The buyer's approved-source data or supplier master (purchasing block) excludes it."""
    return supplier.purchase_blocked or status(supplier) in DISQUALIFIED
