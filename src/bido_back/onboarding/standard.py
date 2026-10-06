"""Bido's standard RFQ template: the `none` source ("Não tenho RFQ"). No model involved.

It matches what the onboarding preview shows for the standard template.
"""

from dataclasses import dataclass, field
from typing import Final


@dataclass(frozen=True, slots=True)
class TemplateContent:
    language: str
    greeting: str
    columns: list[str] = field(default_factory=list[str])
    footer_fields: list[str] = field(default_factory=list[str])
    fields: list[str] = field(default_factory=list[str])

    @property
    def footer(self) -> str:
        return " · ".join(self.footer_fields)


STANDARD_TEMPLATE: Final = TemplateContent(
    language="pt",
    greeting="Olá, gostaríamos de receber a cotação de vocês para os itens abaixo.",
    columns=["Item", "Qtd.", "Un.", "Prazo"],
    footer_fields=["Validade da proposta", "Condição de pagamento"],
    fields=[
        "Item e descrição",
        "Quantidade",
        "Unidade",
        "Prazo de entrega",
        "Condição de pagamento",
        "Validade da proposta",
    ],
)
