"""The phrase catalog: every fixed sentence of a supplier email, per language.

The catalog is versioned code. Portuguese (Brazil) and English were written
with care. German, French, Italian and Spanish are best effort and NEED REVIEW
BY A NATIVE SPEAKER before they are relied on; the golden tests freeze them, so
a reviewer's change shows up as a diff.

Rules every entry follows (`tests/supplier_email/test_catalog.py` checks them):

- every key exists in every language, with the same placeholders (only the
  English salutation names the supplier);
- no digits: every number comes from the data, through the formatters;
- nothing commits: no acceptance or order wording that
  `bido_back.mandates.policy.commitment_language` would flag. The legal line
  says the message is not an order without naming a "purchase order";
- French puts a no-break space before `: ; ? !`; Spanish ends the salutation
  with a colon; German closes without a comma.

Placeholders are `{name}` fields filled by `bido_back.supplier_email.render`.
"""

from collections.abc import Mapping
from typing import Final, Literal

from bido_back.supplier_email.locale import NBSP, catalog_language

Key = Literal[
    "label.reminder",
    "label.clarification",
    "label.revised_offer",
    "label.delivery_date",
    "label.bafo",
    "label.not_selected",
    "purpose",
    "purpose.round",
    "salutation",
    "salutation.anonymous",
    "context.reminder",
    "context.thanks.offer",
    "context.thanks.reply",
    "context.clarification.generic",
    "context.clarification.delivery_one",
    "context.clarification.delivery_many",
    "context.revised.price",
    "context.revised.price_lead",
    "context.combined.price",
    "context.combined.price_lead",
    "context.combined.questions",
    "context.competition",
    "context.delivery.one.date",
    "context.delivery.one.lead",
    "context.delivery.many.date",
    "context.delivery.many.lead",
    "context.bafo",
    "context.not_selected.offer",
    "context.not_selected.request",
    "context.not_selected.outcome",
    "context.not_selected.closed",
    "word.only",
    "word.and",
    "identity.position",
    "identity.code",
    "table.position",
    "table.description",
    "table.code",
    "table.quantity",
    "fact.offer",
    "fact.quoted_lead",
    "fact.target",
    "fact.required_delivery",
    "fact.required_lead",
    "fact.missing",
    "fact.confirm",
    "fact.separator",
    "value.by_date",
    "value.at_most",
    "value.price_per_unit",
    "value.price_per_basis",
    "lead.calendar.one",
    "lead.calendar.many",
    "lead.business.one",
    "lead.business.many",
    "lead.approx",
    "lead.plus_few",
    "lead.after_order",
    "field.currency",
    "field.valid_until",
    "field.unit_price",
    "field.quantity",
    "field.lead_time",
    "field_the.currency",
    "field_the.valid_until",
    "positions.one",
    "positions.many",
    "qty.piece",
    "qty.kg",
    "qty.other",
    "ask.quote_or_decline",
    "ask.offer_terms.state",
    "ask.offer_terms.confirm",
    "ask.positions.missing",
    "ask.positions.confirm",
    "ask.positions.both",
    "ask.deliver_by.one",
    "ask.deliver_by.many_same",
    "ask.deliver_by.many_each",
    "ask.meet_lead.one",
    "ask.meet_lead.many_same",
    "ask.meet_lead.many_each",
    "ask.revise_price",
    "ask.best_and_final",
    "reply.by",
    "reply.by_example",
    "reply.no_resend",
    "fill.delivery",
    "fill.lead",
    "fill.price",
    "fill.final_price",
    "blank.date",
    "blank.value",
    "when.day",
    "when.cutoff",
    "legal",
    "closing",
]

Phrases = Mapping[Key, str]

_EN: Final[dict[Key, str]] = {
    "label.reminder": "Reminder",
    "label.clarification": "Request for clarification",
    "label.revised_offer": "Request for a revised offer",
    "label.delivery_date": "Delivery date request",
    "label.bafo": "Request for best and final offer",
    "label.not_selected": "Outcome of request for quotation",
    "purpose": "{label} – {ref}",
    "purpose.round": "{label} – {ref}, round {round}",
    "salutation": "Dear {name} team,",
    "salutation.anonymous": "Dear Sir or Madam,",
    "context.reminder": (
        "Our request for quotation of {date} is still open, and we have not received "
        "your offer yet."
    ),
    "context.thanks.offer": "Thank you for your offer of {date}.",
    "context.thanks.reply": "Thank you for your reply of {date}.",
    "context.clarification.generic": "Before we evaluate it, we need the information below.",
    "context.clarification.delivery_one": (
        "Before we evaluate it, we need to confirm delivery for position {position}."
    ),
    "context.clarification.delivery_many": (
        "Before we evaluate it, we need to confirm delivery for {count} positions."
    ),
    "context.revised.price": "We ask you to review your prices for the positions below.",
    "context.revised.price_lead": (
        "We ask you to review your prices and lead times for the positions below."
    ),
    "context.combined.price": "We ask you to review your prices for {positions}.",
    "context.combined.price_lead": (
        "We ask you to review your prices and lead times for {positions}."
    ),
    "context.combined.questions": (
        "We also need the information below to complete our evaluation."
    ),
    "context.competition": (
        "For these positions, your offer is not the most competitive we have received."
    ),
    "context.delivery.one.date": (
        "This request concerns position {position}{only}: the quoted lead time does not "
        "meet our required date."
    ),
    "context.delivery.one.lead": (
        "This request concerns position {position}{only}: the quoted lead time is longer "
        "than we need."
    ),
    "context.delivery.many.date": (
        "This request concerns positions {positions}{only}: the quoted lead times do not "
        "meet our required dates."
    ),
    "context.delivery.many.lead": (
        "This request concerns positions {positions}{only}: the quoted lead times are "
        "longer than we need."
    ),
    "context.bafo": (
        "This is the final round of this request for quotation: we intend to decide "
        "without further negotiation."
    ),
    "context.not_selected.offer": (
        "Thank you for your offer of {date} and for the time you dedicated to our request."
    ),
    "context.not_selected.request": (
        "Thank you for your attention to our request for quotation of {date}."
    ),
    "context.not_selected.outcome": (
        "After evaluating the offers received, we have decided not to proceed with your "
        "offer this time."
    ),
    "context.not_selected.closed": "This request for quotation is now closed.",
    "word.only": " only",
    "word.and": "and",
    "identity.position": "Pos. {position}",
    "identity.code": "part no. {code}",
    "table.position": "Pos.",
    "table.description": "Description",
    "table.code": "Part no.",
    "table.quantity": "Qty",
    "fact.offer": "Your offer",
    "fact.quoted_lead": "Lead time quoted",
    "fact.target": "Target price",
    "fact.required_delivery": "Required delivery",
    "fact.required_lead": "Required lead time",
    "fact.missing": "Missing",
    "fact.confirm": "To confirm",
    "fact.separator": ": ",
    "value.by_date": "by {date}",
    "value.at_most": "at most {lead}",
    "value.price_per_unit": "{price}/{unit}",
    "value.price_per_basis": "{price} per {quantity}",
    "lead.calendar.one": "{n} day",
    "lead.calendar.many": "{n} days",
    "lead.business.one": "{n} business day",
    "lead.business.many": "{n} business days",
    "lead.approx": "approx. {lead}",
    "lead.plus_few": "{lead} + a few additional days",
    "lead.after_order": "{lead} after order",
    "field.currency": "currency",
    "field.valid_until": "offer validity",
    "field.unit_price": "unit price",
    "field.quantity": "quantity the price applies to",
    "field.lead_time": "lead time",
    "field_the.currency": "the currency",
    "field_the.valid_until": "the validity",
    "positions.one": "position {position}",
    "positions.many": "positions {positions}",
    "qty.piece": "all {n} pcs",
    "qty.kg": "all {n} kg",
    "qty.other": "the full quantity ({quantity})",
    "ask.quote_or_decline": (
        "Can you send us your quotation? If you will not quote, please let us know."
    ),
    "ask.offer_terms.state": "Please state {fields} of your offer.",
    "ask.offer_terms.confirm": "Please confirm {fields} of your offer.",
    "ask.positions.missing": "For each position above, please send the values marked as missing.",
    "ask.positions.confirm": "For each position above, please confirm the values marked.",
    "ask.positions.both": (
        "For each position above, please send the missing values and confirm the other "
        "values marked."
    ),
    "ask.deliver_by.one": (
        "Can you deliver {quantity} by {date}? If not, what is the earliest date you can confirm?"
    ),
    "ask.deliver_by.many_same": (
        "Can you deliver positions {positions} by {date}? If not, please give the delivery "
        "date you can confirm for each."
    ),
    "ask.deliver_by.many_each": (
        "Can you deliver each position by the required date shown above? If not, please give "
        "the delivery date you can confirm for each."
    ),
    "ask.meet_lead.one": (
        "Can you deliver position {position} within {days} of the order? If not, what is the "
        "shortest lead time you can confirm?"
    ),
    "ask.meet_lead.many_same": (
        "Can you deliver positions {positions} within {days} of the order? If not, please "
        "give the lead time you can confirm for each."
    ),
    "ask.meet_lead.many_each": (
        "Can you meet the required lead time shown above for each position? If not, please "
        "give the lead time you can confirm for each."
    ),
    "ask.revise_price": "Please send a revised offer for {positions}.",
    "ask.best_and_final": (
        "Please send your best and final offer for {positions}, or confirm that your current "
        "offer stands."
    ),
    "reply.by": "Please reply to this email by {when}.",
    "reply.by_example": "Please reply to this email by {when}, for example:",
    "reply.no_resend": "There is no need to resend the other positions.",
    "fill.delivery": "Pos. {position} – delivery by: {blank}",
    "fill.lead": "Pos. {position} – lead time: {blank}",
    "fill.price": "Pos. {position} – revised unit price: {blank}",
    "fill.final_price": "Pos. {position} – final unit price: {blank}",
    "blank.date": "________",
    "blank.value": "________",
    "when.day": "{weekday}, {date}",
    "when.cutoff": "{weekday}, {date}, {time} {zone}",
    "legal": (
        "This message is not an order or an acceptance of your offer. {company} is only "
        "bound by a written order."
    ),
    "closing": "Best regards,",
}

_PT: Final[dict[Key, str]] = {
    "label.reminder": "Lembrete",
    "label.clarification": "Pedido de esclarecimento",
    "label.revised_offer": "Pedido de proposta revisada",
    "label.delivery_date": "Consulta de prazo de entrega",
    "label.bafo": "Pedido de melhor e última oferta",
    "label.not_selected": "Resultado da cotação",
    "purpose": "{label} – {ref}",
    "purpose.round": "{label} – {ref}, rodada {round}",
    "salutation": "Prezados,",
    "salutation.anonymous": "Prezados,",
    "context.reminder": (
        "Nossa solicitação de cotação de {date} continua em aberto e ainda não recebemos "
        "sua proposta."
    ),
    "context.thanks.offer": "Obrigado pela proposta de {date}.",
    "context.thanks.reply": "Obrigado pela resposta de {date}.",
    "context.clarification.generic": "Antes de avaliá-la, precisamos das informações abaixo.",
    "context.clarification.delivery_one": (
        "Antes de avaliá-la, precisamos confirmar a entrega da posição {position}."
    ),
    "context.clarification.delivery_many": (
        "Antes de avaliá-la, precisamos confirmar a entrega de {count} posições."
    ),
    "context.revised.price": "Pedimos a gentileza de revisar os preços das posições abaixo.",
    "context.revised.price_lead": (
        "Pedimos a gentileza de revisar os preços e os prazos das posições abaixo."
    ),
    "context.combined.price": "Pedimos a gentileza de revisar os preços para {positions}.",
    "context.combined.price_lead": (
        "Pedimos a gentileza de revisar os preços e os prazos para {positions}."
    ),
    "context.combined.questions": (
        "Também precisamos das informações abaixo para concluir a avaliação."
    ),
    "context.competition": (
        "Para essas posições, a sua proposta não é a mais competitiva que recebemos."
    ),
    "context.delivery.one.date": (
        "Esta consulta refere-se{only} à posição {position}: o prazo informado não atende à "
        "data de que precisamos."
    ),
    "context.delivery.one.lead": (
        "Esta consulta refere-se{only} à posição {position}: o prazo informado é maior do que "
        "o prazo de que precisamos."
    ),
    "context.delivery.many.date": (
        "Esta consulta refere-se{only} às posições {positions}: os prazos informados não "
        "atendem às datas de que precisamos."
    ),
    "context.delivery.many.lead": (
        "Esta consulta refere-se{only} às posições {positions}: os prazos informados são "
        "maiores do que os prazos de que precisamos."
    ),
    "context.bafo": (
        "Esta é a última rodada desta cotação: pretendemos decidir sem nova negociação."
    ),
    "context.not_selected.offer": (
        "Obrigado pela proposta de {date} e pelo tempo dedicado à nossa cotação."
    ),
    "context.not_selected.request": (
        "Obrigado pela atenção à nossa solicitação de cotação de {date}."
    ),
    "context.not_selected.outcome": (
        "Após avaliar as propostas recebidas, decidimos não seguir com a sua proposta desta vez."
    ),
    "context.not_selected.closed": "Esta cotação está encerrada.",
    "word.only": " somente",
    "word.and": "e",
    "identity.position": "Pos. {position}",
    "identity.code": "cód. {code}",
    "table.position": "Pos.",
    "table.description": "Descrição",
    "table.code": "Cód.",
    "table.quantity": "Qtd.",
    "fact.offer": "Sua proposta",
    "fact.quoted_lead": "Prazo informado",
    "fact.target": "Preço-alvo",
    "fact.required_delivery": "Entrega necessária",
    "fact.required_lead": "Prazo necessário",
    "fact.missing": "Falta informar",
    "fact.confirm": "A confirmar",
    "fact.separator": ": ",
    "value.by_date": "até {date}",
    "value.at_most": "no máximo {lead}",
    "value.price_per_unit": "{price}/{unit}",
    "value.price_per_basis": "{price} por {quantity}",
    "lead.calendar.one": "{n} dia",
    "lead.calendar.many": "{n} dias",
    "lead.business.one": "{n} dia útil",
    "lead.business.many": "{n} dias úteis",
    "lead.approx": "aprox. {lead}",
    "lead.plus_few": "{lead} + alguns dias adicionais",
    "lead.after_order": "{lead} após o pedido",
    "field.currency": "moeda",
    "field.valid_until": "validade da proposta",
    "field.unit_price": "preço unitário",
    "field.quantity": "quantidade a que o preço se aplica",
    "field.lead_time": "prazo de entrega",
    "field_the.currency": "a moeda",
    "field_the.valid_until": "a validade",
    "positions.one": "a posição {position}",
    "positions.many": "as posições {positions}",
    "qty.piece": "as {n} peças",
    "qty.kg": "os {n} kg",
    "qty.other": "a quantidade total ({quantity})",
    "ask.quote_or_decline": (
        "Vocês poderão nos enviar uma cotação? Caso não pretendam cotar, pedimos que nos avisem."
    ),
    "ask.offer_terms.state": "Por favor, informe {fields} da proposta.",
    "ask.offer_terms.confirm": "Por favor, confirme {fields} da proposta.",
    "ask.positions.missing": "Para cada posição acima, por favor, informe os dados que faltam.",
    "ask.positions.confirm": "Para cada posição acima, por favor, confirme os dados indicados.",
    "ask.positions.both": (
        "Para cada posição acima, por favor, informe os dados que faltam e confirme os demais "
        "dados indicados."
    ),
    "ask.deliver_by.one": (
        "Vocês conseguem entregar {quantity} até {date}? Se não, qual é a data mais próxima "
        "que podem confirmar?"
    ),
    "ask.deliver_by.many_same": (
        "Vocês conseguem entregar as posições {positions} até {date}? Se não, informe a data "
        "de entrega que podem confirmar para cada uma."
    ),
    "ask.deliver_by.many_each": (
        "Vocês conseguem entregar cada posição até a data necessária indicada acima? Se não, "
        "informe a data de entrega que podem confirmar para cada uma."
    ),
    "ask.meet_lead.one": (
        "Vocês conseguem entregar a posição {position} em até {days} após o pedido? Se não, "
        "qual é o menor prazo que podem confirmar?"
    ),
    "ask.meet_lead.many_same": (
        "Vocês conseguem entregar as posições {positions} em até {days} após o pedido? Se "
        "não, informe o prazo que podem confirmar para cada uma."
    ),
    "ask.meet_lead.many_each": (
        "Vocês conseguem atender ao prazo necessário indicado acima para cada posição? Se "
        "não, informe o prazo que podem confirmar para cada uma."
    ),
    "ask.revise_price": "Por favor, envie uma proposta revisada para {positions}.",
    "ask.best_and_final": (
        "Por favor, envie sua melhor e última oferta para {positions} ou confirme que a "
        "proposta atual se mantém."
    ),
    "reply.by": "Por favor, responda a este e-mail até {when}.",
    "reply.by_example": "Por favor, responda a este e-mail até {when}, por exemplo:",
    "reply.no_resend": "Não é necessário reenviar as demais posições.",
    "fill.delivery": "Pos. {position} – entrega até: {blank}",
    "fill.lead": "Pos. {position} – prazo de entrega: {blank}",
    "fill.price": "Pos. {position} – preço unitário revisado: {blank}",
    "fill.final_price": "Pos. {position} – preço unitário final: {blank}",
    "blank.date": "__/__/____",
    "blank.value": "________",
    "when.day": "{weekday}, {date}",
    "when.cutoff": "{weekday}, {date}, às {time} ({zone})",
    "legal": (
        "Esta mensagem não é um pedido nem um aceite da sua proposta; a {company} só se "
        "compromete por meio de pedido formal por escrito."
    ),
    "closing": "Atenciosamente,",
}

# German: NEEDS NATIVE REVIEW.
_DE: Final[dict[Key, str]] = {
    "label.reminder": "Erinnerung",
    "label.clarification": "Bitte um Klärung",
    "label.revised_offer": "Bitte um ein überarbeitetes Angebot",
    "label.delivery_date": "Anfrage zum Liefertermin",
    "label.bafo": "Bitte um Ihr letztes und bestes Angebot",
    "label.not_selected": "Ergebnis der Angebotsanfrage",
    "purpose": "{label} – {ref}",
    "purpose.round": "{label} – {ref}, Runde {round}",
    "salutation": "Sehr geehrte Damen und Herren,",
    "salutation.anonymous": "Sehr geehrte Damen und Herren,",
    "context.reminder": (
        "Unsere Angebotsanfrage vom {date} ist noch offen, und Ihr Angebot liegt uns noch "
        "nicht vor."
    ),
    "context.thanks.offer": "Vielen Dank für Ihr Angebot vom {date}.",
    "context.thanks.reply": "Vielen Dank für Ihre Antwort vom {date}.",
    "context.clarification.generic": "Für die Prüfung benötigen wir noch die folgenden Angaben.",
    "context.clarification.delivery_one": (
        "Für die Prüfung müssen wir noch die Lieferung von Position {position} klären."
    ),
    "context.clarification.delivery_many": (
        "Für die Prüfung müssen wir noch die Lieferung von {count} Positionen klären."
    ),
    "context.revised.price": (
        "Wir bitten Sie, Ihre Preise für die folgenden Positionen zu überprüfen."
    ),
    "context.revised.price_lead": (
        "Wir bitten Sie, Ihre Preise und Lieferzeiten für die folgenden Positionen zu überprüfen."
    ),
    "context.combined.price": "Wir bitten Sie, Ihre Preise für {positions} zu überprüfen.",
    "context.combined.price_lead": (
        "Wir bitten Sie, Ihre Preise und Lieferzeiten für {positions} zu überprüfen."
    ),
    "context.combined.questions": ("Für die Prüfung benötigen wir außerdem die folgenden Angaben."),
    "context.competition": (
        "Für diese Positionen ist Ihr Angebot nicht das wettbewerbsfähigste, das uns vorliegt."
    ),
    "context.delivery.one.date": (
        "Diese Anfrage betrifft{only} Position {position}: Die genannte Lieferzeit erfüllt "
        "unseren benötigten Termin nicht."
    ),
    "context.delivery.one.lead": (
        "Diese Anfrage betrifft{only} Position {position}: Die genannte Lieferzeit ist länger "
        "als benötigt."
    ),
    "context.delivery.many.date": (
        "Diese Anfrage betrifft{only} die Positionen {positions}: Die genannten Lieferzeiten "
        "erfüllen unsere benötigten Termine nicht."
    ),
    "context.delivery.many.lead": (
        "Diese Anfrage betrifft{only} die Positionen {positions}: Die genannten Lieferzeiten "
        "sind länger als benötigt."
    ),
    "context.bafo": (
        "Dies ist die letzte Runde dieser Angebotsanfrage: Wir beabsichtigen, ohne weitere "
        "Verhandlung zu entscheiden."
    ),
    "context.not_selected.offer": (
        "Vielen Dank für Ihr Angebot vom {date} und die Zeit, die Sie unserer Anfrage "
        "gewidmet haben."
    ),
    "context.not_selected.request": (
        "Vielen Dank für Ihre Aufmerksamkeit für unsere Angebotsanfrage vom {date}."
    ),
    "context.not_selected.outcome": (
        "Nach Prüfung der eingegangenen Angebote haben wir uns entschieden, Ihr Angebot "
        "diesmal nicht weiterzuverfolgen."
    ),
    "context.not_selected.closed": "Diese Angebotsanfrage ist abgeschlossen.",
    "word.only": " nur",
    "word.and": "und",
    "identity.position": "Pos. {position}",
    "identity.code": "Art.-Nr. {code}",
    "table.position": "Pos.",
    "table.description": "Bezeichnung",
    "table.code": "Art.-Nr.",
    "table.quantity": "Menge",
    "fact.offer": "Ihr Angebot",
    "fact.quoted_lead": "Genannte Lieferzeit",
    "fact.target": "Zielpreis",
    "fact.required_delivery": "Benötigte Lieferung",
    "fact.required_lead": "Benötigte Lieferzeit",
    "fact.missing": "Fehlt",
    "fact.confirm": "Bitte bestätigen",
    "fact.separator": ": ",
    "value.by_date": "bis {date}",
    "value.at_most": "höchstens {lead}",
    "value.price_per_unit": "{price}/{unit}",
    "value.price_per_basis": "{price} pro {quantity}",
    "lead.calendar.one": "{n} Tag",
    "lead.calendar.many": "{n} Tage",
    "lead.business.one": "{n} Arbeitstag",
    "lead.business.many": "{n} Arbeitstage",
    "lead.approx": "ca. {lead}",
    "lead.plus_few": "{lead} + einige zusätzliche Tage",
    "lead.after_order": "{lead} nach Auftragserteilung",
    "field.currency": "Währung",
    "field.valid_until": "Angebotsgültigkeit",
    "field.unit_price": "Stückpreis",
    "field.quantity": "Menge, für die der Preis gilt",
    "field.lead_time": "Lieferzeit",
    "field_the.currency": "die Währung",
    "field_the.valid_until": "die Gültigkeit",
    "positions.one": "Position {position}",
    "positions.many": "die Positionen {positions}",
    "qty.piece": "alle {n} Stück",
    "qty.kg": "die gesamten {n} kg",
    "qty.other": "die gesamte Menge ({quantity})",
    "ask.quote_or_decline": (
        "Können Sie uns ein Angebot senden? Falls Sie kein Angebot abgeben möchten, teilen "
        "Sie uns dies bitte mit."
    ),
    "ask.offer_terms.state": "Bitte nennen Sie uns {fields} Ihres Angebots.",
    "ask.offer_terms.confirm": "Bitte bestätigen Sie {fields} Ihres Angebots.",
    "ask.positions.missing": ("Bitte ergänzen Sie für jede Position oben die fehlenden Angaben."),
    "ask.positions.confirm": (
        "Bitte bestätigen Sie für jede Position oben die markierten Angaben."
    ),
    "ask.positions.both": (
        "Bitte ergänzen Sie für jede Position oben die fehlenden Angaben und bestätigen Sie "
        "die übrigen markierten Angaben."
    ),
    "ask.deliver_by.one": (
        "Können Sie {quantity} bis {date} liefern? Falls nicht, welchen frühesten Termin "
        "können Sie bestätigen?"
    ),
    "ask.deliver_by.many_same": (
        "Können Sie die Positionen {positions} bis {date} liefern? Falls nicht, nennen Sie "
        "bitte für jede Position den Liefertermin, den Sie bestätigen können."
    ),
    "ask.deliver_by.many_each": (
        "Können Sie jede Position bis zum oben genannten Termin liefern? Falls nicht, nennen "
        "Sie bitte für jede Position den Liefertermin, den Sie bestätigen können."
    ),
    "ask.meet_lead.one": (
        "Können Sie Position {position} spätestens {days} nach Auftragserteilung liefern? "
        "Falls nicht, welche kürzeste Lieferzeit können Sie bestätigen?"
    ),
    "ask.meet_lead.many_same": (
        "Können Sie die Positionen {positions} spätestens {days} nach Auftragserteilung "
        "liefern? Falls nicht, nennen Sie bitte für jede Position die Lieferzeit, die Sie "
        "bestätigen können."
    ),
    "ask.meet_lead.many_each": (
        "Können Sie die oben genannte Lieferzeit für jede Position einhalten? Falls nicht, "
        "nennen Sie bitte für jede Position die Lieferzeit, die Sie bestätigen können."
    ),
    "ask.revise_price": "Bitte senden Sie uns ein überarbeitetes Angebot für {positions}.",
    "ask.best_and_final": (
        "Bitte senden Sie uns Ihr letztes und bestes Angebot für {positions} oder bestätigen "
        "Sie, dass Ihr aktuelles Angebot gilt."
    ),
    "reply.by": "Bitte antworten Sie auf diese E-Mail bis {when}.",
    "reply.by_example": "Bitte antworten Sie auf diese E-Mail bis {when}, zum Beispiel:",
    "reply.no_resend": "Die übrigen Positionen müssen Sie nicht erneut senden.",
    "fill.delivery": "Pos. {position} – Lieferung bis: {blank}",
    "fill.lead": "Pos. {position} – Lieferzeit: {blank}",
    "fill.price": "Pos. {position} – überarbeiteter Stückpreis: {blank}",
    "fill.final_price": "Pos. {position} – endgültiger Stückpreis: {blank}",
    "blank.date": "__.__.____",
    "blank.value": "________",
    "when.day": "{weekday}, {date}",
    "when.cutoff": "{weekday}, {date}, {time} Uhr ({zone})",
    "legal": (
        "Diese Nachricht ist weder ein Auftrag noch die Annahme Ihres Angebots; {company} "
        "ist nur durch einen schriftlichen Auftrag gebunden."
    ),
    "closing": "Mit freundlichen Grüßen",
}

# French: NEEDS NATIVE REVIEW. A no-break space precedes : ; ? !
_FR: Final[dict[Key, str]] = {
    "label.reminder": "Relance",
    "label.clarification": "Demande de précisions",
    "label.revised_offer": "Demande d'offre révisée",
    "label.delivery_date": "Demande de délai de livraison",
    "label.bafo": "Demande de meilleure offre finale",
    "label.not_selected": "Résultat de la demande de prix",
    "purpose": "{label} – {ref}",
    "purpose.round": "{label} – {ref}, tour {round}",
    "salutation": "Bonjour,",
    "salutation.anonymous": "Bonjour,",
    "context.reminder": (
        "Notre demande de prix du {date} est toujours ouverte et nous n'avons pas encore "
        "reçu votre offre."
    ),
    "context.thanks.offer": "Merci pour votre offre du {date}.",
    "context.thanks.reply": "Merci pour votre réponse du {date}.",
    "context.clarification.generic": (
        "Avant de l'évaluer, nous avons besoin des informations ci-dessous."
    ),
    "context.clarification.delivery_one": (
        "Avant de l'évaluer, nous devons confirmer la livraison de la position {position}."
    ),
    "context.clarification.delivery_many": (
        "Avant de l'évaluer, nous devons confirmer la livraison de {count} positions."
    ),
    "context.revised.price": (
        "Nous vous demandons de revoir vos prix pour les positions ci-dessous."
    ),
    "context.revised.price_lead": (
        "Nous vous demandons de revoir vos prix et vos délais pour les positions ci-dessous."
    ),
    "context.combined.price": "Nous vous demandons de revoir vos prix pour {positions}.",
    "context.combined.price_lead": (
        "Nous vous demandons de revoir vos prix et vos délais pour {positions}."
    ),
    "context.combined.questions": (
        "Nous avons également besoin des informations ci-dessous pour terminer notre évaluation."
    ),
    "context.competition": (
        "Pour ces positions, votre offre n'est pas la plus compétitive que nous ayons reçue."
    ),
    "context.delivery.one.date": (
        f"Cette demande concerne{{only}} la position {{position}}{NBSP}: le délai indiqué ne "
        "permet pas de respecter la date requise."
    ),
    "context.delivery.one.lead": (
        f"Cette demande concerne{{only}} la position {{position}}{NBSP}: le délai indiqué est "
        "plus long que nécessaire."
    ),
    "context.delivery.many.date": (
        f"Cette demande concerne{{only}} les positions {{positions}}{NBSP}: les délais "
        "indiqués ne permettent pas de respecter les dates requises."
    ),
    "context.delivery.many.lead": (
        f"Cette demande concerne{{only}} les positions {{positions}}{NBSP}: les délais "
        "indiqués sont plus longs que nécessaire."
    ),
    "context.bafo": (
        f"Il s'agit du dernier tour de cette demande de prix{NBSP}: nous prévoyons de "
        "décider sans autre négociation."
    ),
    "context.not_selected.offer": (
        "Merci pour votre offre du {date} et pour le temps consacré à notre demande."
    ),
    "context.not_selected.request": (
        "Merci de l'attention portée à notre demande de prix du {date}."
    ),
    "context.not_selected.outcome": (
        "Après évaluation des offres reçues, nous avons décidé de ne pas retenir votre offre "
        "cette fois-ci."
    ),
    "context.not_selected.closed": "Cette demande de prix est désormais close.",
    "word.only": " uniquement",
    "word.and": "et",
    "identity.position": "Pos. {position}",
    "identity.code": "réf. {code}",
    "table.position": "Pos.",
    "table.description": "Désignation",
    "table.code": "Réf.",
    "table.quantity": "Qté",
    "fact.offer": "Votre offre",
    "fact.quoted_lead": "Délai indiqué",
    "fact.target": "Prix cible",
    "fact.required_delivery": "Livraison requise",
    "fact.required_lead": "Délai requis",
    "fact.missing": "Manquant",
    "fact.confirm": "À confirmer",
    "fact.separator": f"{NBSP}: ",
    "value.by_date": "au plus tard le {date}",
    "value.at_most": "au maximum {lead}",
    "value.price_per_unit": "{price}/{unit}",
    "value.price_per_basis": "{price} pour {quantity}",
    "lead.calendar.one": "{n} jour",
    "lead.calendar.many": "{n} jours",
    "lead.business.one": "{n} jour ouvré",
    "lead.business.many": "{n} jours ouvrés",
    "lead.approx": "env. {lead}",
    "lead.plus_few": "{lead} + quelques jours supplémentaires",
    "lead.after_order": "{lead} après la commande",
    "field.currency": "devise",
    "field.valid_until": "validité de l'offre",
    "field.unit_price": "prix unitaire",
    "field.quantity": "quantité à laquelle le prix s'applique",
    "field.lead_time": "délai de livraison",
    "field_the.currency": "la devise",
    "field_the.valid_until": "la durée de validité",
    "positions.one": "la position {position}",
    "positions.many": "les positions {positions}",
    "qty.piece": "les {n} pièces",
    "qty.kg": "les {n} kg",
    "qty.other": "la quantité totale ({quantity})",
    "ask.quote_or_decline": (
        f"Pouvez-vous nous envoyer votre offre{NBSP}? Si vous ne souhaitez pas répondre, "
        "merci de nous le faire savoir."
    ),
    "ask.offer_terms.state": "Merci de nous indiquer {fields} de votre offre.",
    "ask.offer_terms.confirm": "Merci de nous confirmer {fields} de votre offre.",
    "ask.positions.missing": (
        "Pour chaque position ci-dessus, merci de nous indiquer les informations manquantes."
    ),
    "ask.positions.confirm": (
        "Pour chaque position ci-dessus, merci de confirmer les informations signalées."
    ),
    "ask.positions.both": (
        "Pour chaque position ci-dessus, merci de nous indiquer les informations manquantes "
        "et de confirmer les autres informations signalées."
    ),
    "ask.deliver_by.one": (
        f"Pouvez-vous livrer {{quantity}} au plus tard le {{date}}{NBSP}? Sinon, quelle est "
        f"la date la plus proche que vous pouvez confirmer{NBSP}?"
    ),
    "ask.deliver_by.many_same": (
        f"Pouvez-vous livrer les positions {{positions}} au plus tard le {{date}}{NBSP}? "
        "Sinon, merci d'indiquer pour chacune la date de livraison que vous pouvez confirmer."
    ),
    "ask.deliver_by.many_each": (
        f"Pouvez-vous livrer chaque position à la date requise indiquée ci-dessus{NBSP}? "
        "Sinon, merci d'indiquer pour chacune la date de livraison que vous pouvez confirmer."
    ),
    "ask.meet_lead.one": (
        f"Pouvez-vous livrer la position {{position}} dans un délai de {{days}} après la "
        f"commande{NBSP}? Sinon, quel est le délai le plus court que vous pouvez "
        f"confirmer{NBSP}?"
    ),
    "ask.meet_lead.many_same": (
        f"Pouvez-vous livrer les positions {{positions}} dans un délai de {{days}} après la "
        f"commande{NBSP}? Sinon, merci d'indiquer pour chacune le délai que vous pouvez "
        "confirmer."
    ),
    "ask.meet_lead.many_each": (
        f"Pouvez-vous respecter le délai requis indiqué ci-dessus pour chaque "
        f"position{NBSP}? Sinon, merci d'indiquer pour chacune le délai que vous pouvez "
        "confirmer."
    ),
    "ask.revise_price": "Merci de nous envoyer une offre révisée pour {positions}.",
    "ask.best_and_final": (
        "Merci de nous envoyer votre meilleure offre finale pour {positions}, ou de confirmer "
        "que votre offre actuelle est maintenue."
    ),
    "reply.by": "Merci de répondre à cet e-mail au plus tard le {when}.",
    "reply.by_example": (
        f"Merci de répondre à cet e-mail au plus tard le {{when}}, par exemple{NBSP}:"
    ),
    "reply.no_resend": "Il n'est pas nécessaire de renvoyer les autres positions.",
    "fill.delivery": f"Pos. {{position}} – livraison au plus tard le{NBSP}: {{blank}}",
    "fill.lead": f"Pos. {{position}} – délai de livraison{NBSP}: {{blank}}",
    "fill.price": f"Pos. {{position}} – prix unitaire révisé{NBSP}: {{blank}}",
    "fill.final_price": f"Pos. {{position}} – prix unitaire final{NBSP}: {{blank}}",
    "blank.date": "__/__/____",
    "blank.value": "________",
    "when.day": "{weekday} {date}",
    "when.cutoff": "{weekday} {date} à {time} ({zone})",
    "legal": (
        f"Ce message ne constitue ni une commande ni une acceptation de votre offre{NBSP}; "
        "{company} n'est engagée que par une commande écrite."
    ),
    "closing": "Cordialement,",
}

# Italian: NEEDS NATIVE REVIEW.
_IT: Final[dict[Key, str]] = {
    "label.reminder": "Sollecito",
    "label.clarification": "Richiesta di chiarimenti",
    "label.revised_offer": "Richiesta di offerta rivista",
    "label.delivery_date": "Richiesta di data di consegna",
    "label.bafo": "Richiesta di migliore offerta finale",
    "label.not_selected": "Esito della richiesta di offerta",
    "purpose": "{label} – {ref}",
    "purpose.round": "{label} – {ref}, turno {round}",
    "salutation": "Buongiorno,",
    "salutation.anonymous": "Buongiorno,",
    "context.reminder": (
        "La nostra richiesta di offerta del {date} è ancora aperta e non abbiamo ancora "
        "ricevuto la Vostra offerta."
    ),
    "context.thanks.offer": "Grazie per la Vostra offerta del {date}.",
    "context.thanks.reply": "Grazie per la Vostra risposta del {date}.",
    "context.clarification.generic": (
        "Prima di valutarla, abbiamo bisogno delle informazioni seguenti."
    ),
    "context.clarification.delivery_one": (
        "Prima di valutarla, dobbiamo confermare la consegna della posizione {position}."
    ),
    "context.clarification.delivery_many": (
        "Prima di valutarla, dobbiamo confermare la consegna di {count} posizioni."
    ),
    "context.revised.price": "Vi chiediamo di rivedere i prezzi delle posizioni seguenti.",
    "context.revised.price_lead": (
        "Vi chiediamo di rivedere i prezzi e i tempi di consegna delle posizioni seguenti."
    ),
    "context.combined.price": "Vi chiediamo di rivedere i prezzi per {positions}.",
    "context.combined.price_lead": (
        "Vi chiediamo di rivedere i prezzi e i tempi di consegna per {positions}."
    ),
    "context.combined.questions": (
        "Abbiamo inoltre bisogno delle informazioni seguenti per completare la valutazione."
    ),
    "context.competition": (
        "Per queste posizioni, la Vostra offerta non è la più competitiva tra quelle ricevute."
    ),
    "context.delivery.one.date": (
        "Questa richiesta riguarda{only} la posizione {position}: il termine indicato non "
        "rispetta la data richiesta."
    ),
    "context.delivery.one.lead": (
        "Questa richiesta riguarda{only} la posizione {position}: il termine indicato è più "
        "lungo del necessario."
    ),
    "context.delivery.many.date": (
        "Questa richiesta riguarda{only} le posizioni {positions}: i termini indicati non "
        "rispettano le date richieste."
    ),
    "context.delivery.many.lead": (
        "Questa richiesta riguarda{only} le posizioni {positions}: i termini indicati sono "
        "più lunghi del necessario."
    ),
    "context.bafo": (
        "Questo è l'ultimo turno di questa richiesta di offerta: intendiamo decidere senza "
        "ulteriori trattative."
    ),
    "context.not_selected.offer": (
        "Grazie per la Vostra offerta del {date} e per il tempo dedicato alla nostra richiesta."
    ),
    "context.not_selected.request": (
        "Grazie per l'attenzione dedicata alla nostra richiesta di offerta del {date}."
    ),
    "context.not_selected.outcome": (
        "Dopo aver valutato le offerte ricevute, abbiamo deciso di non procedere con la Vostra "
        "offerta questa volta."
    ),
    "context.not_selected.closed": "Questa richiesta di offerta è ora chiusa.",
    "word.only": " solo",
    "word.and": "e",
    "identity.position": "Pos. {position}",
    "identity.code": "cod. {code}",
    "table.position": "Pos.",
    "table.description": "Descrizione",
    "table.code": "Cod.",
    "table.quantity": "Q.tà",
    "fact.offer": "La Vostra offerta",
    "fact.quoted_lead": "Termine indicato",
    "fact.target": "Prezzo obiettivo",
    "fact.required_delivery": "Consegna richiesta",
    "fact.required_lead": "Termine richiesto",
    "fact.missing": "Mancante",
    "fact.confirm": "Da confermare",
    "fact.separator": ": ",
    "value.by_date": "entro il {date}",
    "value.at_most": "al massimo {lead}",
    "value.price_per_unit": "{price}/{unit}",
    "value.price_per_basis": "{price} per {quantity}",
    "lead.calendar.one": "{n} giorno",
    "lead.calendar.many": "{n} giorni",
    "lead.business.one": "{n} giorno lavorativo",
    "lead.business.many": "{n} giorni lavorativi",
    "lead.approx": "circa {lead}",
    "lead.plus_few": "{lead} + alcuni giorni aggiuntivi",
    "lead.after_order": "{lead} dall'ordine",
    "field.currency": "valuta",
    "field.valid_until": "validità dell'offerta",
    "field.unit_price": "prezzo unitario",
    "field.quantity": "quantità a cui si applica il prezzo",
    "field.lead_time": "termine di consegna",
    "field_the.currency": "la valuta",
    "field_the.valid_until": "la validità",
    "positions.one": "la posizione {position}",
    "positions.many": "le posizioni {positions}",
    "qty.piece": "tutti i {n} pezzi",
    "qty.kg": "tutti i {n} kg",
    "qty.other": "l'intera quantità ({quantity})",
    "ask.quote_or_decline": (
        "Potete inviarci la Vostra offerta? Se non intendete quotare, Vi preghiamo di "
        "comunicarcelo."
    ),
    "ask.offer_terms.state": "Vi preghiamo di indicarci {fields} della Vostra offerta.",
    "ask.offer_terms.confirm": "Vi preghiamo di confermarci {fields} della Vostra offerta.",
    "ask.positions.missing": (
        "Per ogni posizione indicata sopra, Vi preghiamo di comunicarci i dati mancanti."
    ),
    "ask.positions.confirm": (
        "Per ogni posizione indicata sopra, Vi preghiamo di confermare i dati segnalati."
    ),
    "ask.positions.both": (
        "Per ogni posizione indicata sopra, Vi preghiamo di comunicarci i dati mancanti e di "
        "confermare gli altri dati segnalati."
    ),
    "ask.deliver_by.one": (
        "Potete consegnare {quantity} entro il {date}? In caso contrario, qual è la data più "
        "vicina che potete confermare?"
    ),
    "ask.deliver_by.many_same": (
        "Potete consegnare le posizioni {positions} entro il {date}? In caso contrario, "
        "indicateci per ciascuna la data di consegna che potete confermare."
    ),
    "ask.deliver_by.many_each": (
        "Potete consegnare ogni posizione entro la data richiesta indicata sopra? In caso "
        "contrario, indicateci per ciascuna la data di consegna che potete confermare."
    ),
    "ask.meet_lead.one": (
        "Potete consegnare la posizione {position} entro {days} dall'ordine? In caso "
        "contrario, qual è il termine più breve che potete confermare?"
    ),
    "ask.meet_lead.many_same": (
        "Potete consegnare le posizioni {positions} entro {days} dall'ordine? In caso "
        "contrario, indicateci per ciascuna il termine che potete confermare."
    ),
    "ask.meet_lead.many_each": (
        "Potete rispettare il termine richiesto indicato sopra per ogni posizione? In caso "
        "contrario, indicateci per ciascuna il termine che potete confermare."
    ),
    "ask.revise_price": "Vi preghiamo di inviarci un'offerta rivista per {positions}.",
    "ask.best_and_final": (
        "Vi preghiamo di inviarci la Vostra migliore offerta finale per {positions}, oppure "
        "di confermare che l'offerta attuale resta valida."
    ),
    "reply.by": "Vi preghiamo di rispondere a questa e-mail entro {when}.",
    "reply.by_example": "Vi preghiamo di rispondere a questa e-mail entro {when}, ad esempio:",
    "reply.no_resend": "Non è necessario inviare di nuovo le altre posizioni.",
    "fill.delivery": "Pos. {position} – consegna entro: {blank}",
    "fill.lead": "Pos. {position} – termine di consegna: {blank}",
    "fill.price": "Pos. {position} – prezzo unitario rivisto: {blank}",
    "fill.final_price": "Pos. {position} – prezzo unitario finale: {blank}",
    "blank.date": "__/__/____",
    "blank.value": "________",
    "when.day": "{weekday} {date}",
    "when.cutoff": "{weekday} {date} alle {time} ({zone})",
    "legal": (
        "Il presente messaggio non costituisce un ordine né l'accettazione della Vostra "
        "offerta; {company} è vincolata solo da un ordine scritto."
    ),
    "closing": "Cordiali saluti,",
}

# Spanish: NEEDS NATIVE REVIEW. The salutation ends with a colon.
_ES: Final[dict[Key, str]] = {
    "label.reminder": "Recordatorio",
    "label.clarification": "Solicitud de aclaración",
    "label.revised_offer": "Solicitud de oferta revisada",
    "label.delivery_date": "Consulta de plazo de entrega",
    "label.bafo": "Solicitud de mejor oferta final",
    "label.not_selected": "Resultado de la solicitud de cotización",
    "purpose": "{label} – {ref}",
    "purpose.round": "{label} – {ref}, ronda {round}",
    "salutation": "Estimados señores:",
    "salutation.anonymous": "Estimados señores:",
    "context.reminder": (
        "Nuestra solicitud de cotización del {date} sigue abierta y aún no hemos recibido su "
        "oferta."
    ),
    "context.thanks.offer": "Gracias por su oferta del {date}.",
    "context.thanks.reply": "Gracias por su respuesta del {date}.",
    "context.clarification.generic": "Antes de evaluarla, necesitamos la información siguiente.",
    "context.clarification.delivery_one": (
        "Antes de evaluarla, necesitamos confirmar la entrega de la posición {position}."
    ),
    "context.clarification.delivery_many": (
        "Antes de evaluarla, necesitamos confirmar la entrega de {count} posiciones."
    ),
    "context.revised.price": (
        "Les pedimos que revisen sus precios para las posiciones siguientes."
    ),
    "context.revised.price_lead": (
        "Les pedimos que revisen sus precios y plazos de entrega para las posiciones siguientes."
    ),
    "context.combined.price": "Les pedimos que revisen sus precios para {positions}.",
    "context.combined.price_lead": (
        "Les pedimos que revisen sus precios y plazos de entrega para {positions}."
    ),
    "context.combined.questions": (
        "También necesitamos la información siguiente para completar la evaluación."
    ),
    "context.competition": (
        "Para estas posiciones, su oferta no es la más competitiva que hemos recibido."
    ),
    "context.delivery.one.date": (
        "Esta consulta se refiere{only} a la posición {position}: el plazo indicado no cumple "
        "la fecha requerida."
    ),
    "context.delivery.one.lead": (
        "Esta consulta se refiere{only} a la posición {position}: el plazo indicado es más "
        "largo de lo necesario."
    ),
    "context.delivery.many.date": (
        "Esta consulta se refiere{only} a las posiciones {positions}: los plazos indicados no "
        "cumplen las fechas requeridas."
    ),
    "context.delivery.many.lead": (
        "Esta consulta se refiere{only} a las posiciones {positions}: los plazos indicados son "
        "más largos de lo necesario."
    ),
    "context.bafo": (
        "Esta es la última ronda de esta solicitud de cotización: tenemos la intención de "
        "decidir sin más negociación."
    ),
    "context.not_selected.offer": (
        "Gracias por su oferta del {date} y por el tiempo dedicado a nuestra solicitud."
    ),
    "context.not_selected.request": (
        "Gracias por la atención prestada a nuestra solicitud de cotización del {date}."
    ),
    "context.not_selected.outcome": (
        "Tras evaluar las ofertas recibidas, hemos decidido no continuar con su oferta en esta "
        "ocasión."
    ),
    "context.not_selected.closed": "Esta solicitud de cotización está cerrada.",
    "word.only": " solo",
    "word.and": "y",
    "identity.position": "Pos. {position}",
    "identity.code": "ref. {code}",
    "table.position": "Pos.",
    "table.description": "Descripción",
    "table.code": "Ref.",
    "table.quantity": "Cant.",
    "fact.offer": "Su oferta",
    "fact.quoted_lead": "Plazo indicado",
    "fact.target": "Precio objetivo",
    "fact.required_delivery": "Entrega requerida",
    "fact.required_lead": "Plazo requerido",
    "fact.missing": "Falta",
    "fact.confirm": "Por confirmar",
    "fact.separator": ": ",
    "value.by_date": "a más tardar el {date}",
    "value.at_most": "como máximo {lead}",
    "value.price_per_unit": "{price}/{unit}",
    "value.price_per_basis": "{price} por {quantity}",
    "lead.calendar.one": "{n} día",
    "lead.calendar.many": "{n} días",
    "lead.business.one": "{n} día hábil",
    "lead.business.many": "{n} días hábiles",
    "lead.approx": "aprox. {lead}",
    "lead.plus_few": "{lead} + algunos días adicionales",
    "lead.after_order": "{lead} tras el pedido",
    "field.currency": "moneda",
    "field.valid_until": "validez de la oferta",
    "field.unit_price": "precio unitario",
    "field.quantity": "cantidad a la que se aplica el precio",
    "field.lead_time": "plazo de entrega",
    "field_the.currency": "la moneda",
    "field_the.valid_until": "la validez",
    "positions.one": "la posición {position}",
    "positions.many": "las posiciones {positions}",
    "qty.piece": "las {n} piezas",
    "qty.kg": "los {n} kg",
    "qty.other": "la cantidad total ({quantity})",
    "ask.quote_or_decline": (
        "¿Pueden enviarnos su cotización? Si no van a cotizar, les rogamos que nos lo indiquen."
    ),
    "ask.offer_terms.state": "Por favor, indíquennos {fields} de su oferta.",
    "ask.offer_terms.confirm": "Por favor, confírmennos {fields} de su oferta.",
    "ask.positions.missing": (
        "Para cada posición indicada arriba, por favor, envíennos los datos que faltan."
    ),
    "ask.positions.confirm": (
        "Para cada posición indicada arriba, por favor, confirmen los datos señalados."
    ),
    "ask.positions.both": (
        "Para cada posición indicada arriba, por favor, envíennos los datos que faltan y "
        "confirmen los demás datos señalados."
    ),
    "ask.deliver_by.one": (
        "¿Pueden entregar {quantity} a más tardar el {date}? Si no, ¿cuál es la fecha más "
        "próxima que pueden confirmar?"
    ),
    "ask.deliver_by.many_same": (
        "¿Pueden entregar las posiciones {positions} a más tardar el {date}? Si no, indiquen "
        "para cada una la fecha de entrega que pueden confirmar."
    ),
    "ask.deliver_by.many_each": (
        "¿Pueden entregar cada posición en la fecha requerida indicada arriba? Si no, indiquen "
        "para cada una la fecha de entrega que pueden confirmar."
    ),
    "ask.meet_lead.one": (
        "¿Pueden entregar la posición {position} en un plazo de {days} tras el pedido? Si no, "
        "¿cuál es el plazo más corto que pueden confirmar?"
    ),
    "ask.meet_lead.many_same": (
        "¿Pueden entregar las posiciones {positions} en un plazo de {days} tras el pedido? Si "
        "no, indiquen para cada una el plazo que pueden confirmar."
    ),
    "ask.meet_lead.many_each": (
        "¿Pueden cumplir el plazo requerido indicado arriba para cada posición? Si no, "
        "indiquen para cada una el plazo que pueden confirmar."
    ),
    "ask.revise_price": "Por favor, envíennos una oferta revisada para {positions}.",
    "ask.best_and_final": (
        "Por favor, envíennos su mejor oferta final para {positions}, o confirmen que su "
        "oferta actual se mantiene."
    ),
    "reply.by": "Por favor, respondan a este correo a más tardar el {when}.",
    "reply.by_example": ("Por favor, respondan a este correo a más tardar el {when}, por ejemplo:"),
    "reply.no_resend": "No es necesario reenviar las demás posiciones.",
    "fill.delivery": "Pos. {position} – entrega a más tardar el: {blank}",
    "fill.lead": "Pos. {position} – plazo de entrega: {blank}",
    "fill.price": "Pos. {position} – precio unitario revisado: {blank}",
    "fill.final_price": "Pos. {position} – precio unitario final: {blank}",
    "blank.date": "__/__/____",
    "blank.value": "________",
    "when.day": "{weekday} {date}",
    "when.cutoff": "{weekday} {date} a las {time} ({zone})",
    "legal": (
        "Este mensaje no constituye un pedido ni la aceptación de su oferta; {company} solo "
        "queda obligada mediante un pedido por escrito."
    ),
    "closing": "Saludos cordiales,",
}

CATALOG: Final[Mapping[str, Phrases]] = {
    "pt": _PT,
    "en": _EN,
    "de": _DE,
    "fr": _FR,
    "it": _IT,
    "es": _ES,
}

# Small counts as words ("two positions"), 2 to 10; digits beyond. Feminine where it
# matters: they count positions (posição, posición, posizione, position).
_COUNTS: Final[dict[str, tuple[str, ...]]] = {
    "pt": ("duas", "três", "quatro", "cinco", "seis", "sete", "oito", "nove", "dez"),
    "en": ("two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"),
    "de": ("zwei", "drei", "vier", "fünf", "sechs", "sieben", "acht", "neun", "zehn"),
    "fr": ("deux", "trois", "quatre", "cinq", "six", "sept", "huit", "neuf", "dix"),
    "it": ("due", "tre", "quattro", "cinque", "sei", "sette", "otto", "nove", "dieci"),
    "es": ("dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve", "diez"),
}


def phrases(language: str) -> Phrases:
    """The catalog of a language; English for a language without one (never mixed)."""
    return CATALOG[catalog_language(language)]


def count_word(count: int, language: str) -> str:
    """2 to 10 as a word ("duas", "two"); any other count as digits."""
    words = _COUNTS[catalog_language(language)]
    return words[count - 2] if 2 <= count <= 10 else str(count)


def type_label(kind: str, language: str) -> str:
    """The type label of an email type, e.g. `Pedido de esclarecimento`."""
    key: Key
    match kind:
        case "reminder":
            key = "label.reminder"
        case "clarification":
            key = "label.clarification"
        case "revised_offer":
            key = "label.revised_offer"
        case "delivery_date":
            key = "label.delivery_date"
        case "bafo":
            key = "label.bafo"
        case "not_selected":
            key = "label.not_selected"
        case _:
            raise ValueError(f"unknown email type {kind!r}")
    return phrases(language)[key]
