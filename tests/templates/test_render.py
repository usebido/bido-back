"""The RFQ email render: deterministic, from the template and the RFQ lines."""

from dataclasses import replace
from datetime import date
from decimal import Decimal

from bido_back.agents.catalog.contracts import RfqEmailInput, RfqEmailLine, RfqEmailOutput
from bido_back.agents.catalog.definitions import rfq_cover_problems, rfq_email_problems
from bido_back.onboarding.standard import STANDARD_TEMPLATE, TemplateContent
from bido_back.templates.render import (
    RenderedRfq,
    RenderInput,
    RenderLine,
    column_key,
    render_cover,
    render_rfq,
)

THERMISTOR = RenderLine(
    position=1,
    description="NTC thermistor 10k",
    part_number=None,
    mpn="NCP18XH103F03RB",
    manufacturer="Murata",
    quantities=[Decimal(10000), Decimal("50000.000")],
    unit="pcs",
    need_by=date(2027, 1, 15),
    incoterm="DAP",
    delivery_location="Leipzig",
    qualification="AEC-Q200",
)
CAPACITOR = RenderLine(
    position=2,
    description="MLCC 100nF X7R 0603",
    part_number=None,
    mpn="GCM188R71H104KA57D",
    manufacturer="Murata",
    quantities=[Decimal(20000)],
    unit="pcs",
    need_by=date(2027, 1, 15),
    incoterm="DAP",
    delivery_location="Leipzig",
    qualification=None,
)

BUYER_TEMPLATE = TemplateContent(
    language="pt",
    greeting="Prezados, boa tarde. Solicitamos cotação para os itens abaixo:",
    columns=["Item e descrição", "Qtd", "Un", "Código", "Preço unitário", "Prazo de entrega"],
    footer_fields=["Condição de pagamento", "Validade da proposta", "Frete (CIF/FOB)"],
    fields=[],
)


def render(
    template: TemplateContent,
    *,
    language: str = "pt",
    display: TemplateContent | None = None,
    lines: tuple[RenderLine, ...] = (THERMISTOR, CAPACITOR),
    due: date | None = date(2026, 10, 20),
    payment_terms: str | None = None,
    quote_validity_days: int | None = None,
) -> RenderedRfq:
    return render_rfq(
        RenderInput(
            rfq_ref="RFQ-7",
            rfq_title="BMS board rev C passives",
            response_due=due,
            buyer_name="Carla Souza",
            buyer_company="Voltwerk Ltda",
            language=language,
            template=template,
            display=display or template,
            lines=lines,
            payment_terms=payment_terms,
            quote_validity_days=quote_validity_days,
        )
    )


def table_rows(text: str) -> list[list[str]]:
    rows = [line for line in text.splitlines() if " | " in line]
    return [[cell.strip() for cell in row.split("|")] for row in rows]


def test_headings_map_to_line_data_or_stay_for_the_supplier() -> None:
    assert column_key("Item e descrição") == "description"
    assert column_key("Item") == "position"
    assert column_key("Qtd.") == "quantity"
    assert column_key("Código") == "mpn"
    assert column_key("Prazo") == "need_by"
    assert column_key("Prazo de entrega") is None  # lead time: the supplier answers
    assert column_key("Preço unitário") is None
    assert column_key("Lieferzeit (Wochen)") is None
    # Bido's own headings in other languages name the same line data.
    assert column_key("Data richiesta") == "need_by"
    assert column_key("Date souhaitée") == "need_by"
    assert column_key("Réf. fabricant") == "mpn"
    assert column_key("Délai de livraison") is None
    assert column_key("Delivery time") is None


def test_buyer_columns_in_their_order_then_the_missing_line_data() -> None:
    rendered = render(BUYER_TEMPLATE)

    header, *rows = table_rows(rendered.text)
    assert header == [
        "Item",  # added in front: the template has no position column
        "Item e descrição",
        "Qtd",
        "Un",
        "Código",
        "Preço unitário",
        "Prazo de entrega",
        "Fabricante",
        "Data de entrega",
        "Qualificação",
    ]
    assert rows[0] == [
        "1",
        "NTC thermistor 10k",
        "10000 / 50000",
        "pcs",
        "NCP18XH103F03RB",
        "",
        "",
        "Murata",
        "15/01/2027",
        "AEC-Q200",
    ]
    # One Incoterm for every line: said once, under the table.
    assert "Incoterm e local de entrega: DAP Leipzig" in rendered.text


def test_footer_fields_then_required_questions_the_footer_does_not_cover() -> None:
    rendered = render(BUYER_TEMPLATE)

    asks = [line[2:] for line in rendered.text.splitlines() if line.startswith("- ")]
    assert asks == [
        "Condição de pagamento",
        "Validade da proposta",
        "Frete (CIF/FOB)",
        "Embalagem",
        "Declarações RoHS e REACH",
        # No "Prazo de entrega": the template's column already asks it.
        "Quantidade mínima (MOQ)",
        "Qualificação (ex.: AEC-Q200) e certificados",  # a line asks for AEC-Q200
    ]


def test_the_supplier_language_drives_fixed_phrases_and_the_display_labels() -> None:
    display = TemplateContent(
        language="de",
        greeting="Sehr geehrte Damen und Herren, wir bitten um ein Angebot:",
        columns=["Pos. und Bezeichnung", "Menge", "Einheit", "Code", "Stückpreis", "Lieferzeit"],
        footer_fields=["Zahlungsbedingungen", "Gültigkeit", "Fracht (CIF/FOB)"],
    )

    rendered = render(BUYER_TEMPLATE, language="de", display=display)

    assert rendered.subject == "[RFQ-7] Angebotsanfrage: BMS board rev C passives"
    assert rendered.text.startswith("Sehr geehrte Damen und Herren, wir bitten um ein Angebot:")
    assert "Unsere Referenz: RFQ-7." in rendered.text
    # Columns Bido fills get its own heading in the supplier's language.
    assert table_rows(rendered.text)[0][:3] == ["Pos.", "Bezeichnung", "Menge"]
    assert "- Zahlungsbedingungen" in rendered.text  # the buyer's field, localized
    assert "- Verpackungseinheit" in rendered.text  # a required one, from our labels
    assert "Bitte senden Sie Ihr Angebot bis 20.10.2026." in rendered.text
    assert rendered.text.endswith("Mit freundlichen Grüßen\nCarla Souza\nVoltwerk Ltda\n")


def test_standard_template_passes_the_email_checks() -> None:
    rendered = render(STANDARD_TEMPLATE, language="en")
    task = RfqEmailInput(
        rfq_ref="RFQ-7",
        rfq_title="BMS board rev C passives",
        buyer_company="Voltwerk Ltda",
        buyer_name="Carla Souza",
        supplier_name="Rhein",
        supplier_language="en",
        lines=[
            RfqEmailLine(
                position=line.position,
                description=line.description,
                mpn=line.mpn,
                quantities=list(line.quantities),
                unit=line.unit,
            )
            for line in (THERMISTOR, CAPACITOR)
        ],
    )

    output = RfqEmailOutput(subject=rendered.subject, body=rendered.text)
    assert rfq_email_problems(task, output) == []


def test_mixed_incoterms_go_per_line_and_no_due_date_no_due_sentence() -> None:
    ex_works = RenderLine(
        position=3,
        description="Connector",
        part_number=None,
        mpn=None,
        manufacturer=None,
        quantities=[Decimal(500)],
        unit="pcs",
        need_by=None,
        incoterm="EXW",
        delivery_location=None,
        qualification=None,
    )

    rendered = render(STANDARD_TEMPLATE, language="en", lines=(CAPACITOR, ex_works), due=None)

    header, first, second = table_rows(rendered.text)
    assert header[-1] == "Incoterm"
    assert (first[-1], second[-1]) == ("DAP Leipzig", "EXW")
    assert "Please send your quote by" not in rendered.text


def test_html_escapes_the_buyers_text() -> None:
    template = TemplateContent(
        language="en",
        greeting="Hello <script>alert(1)</script> & welcome",
        columns=["Item"],
        footer_fields=["Terms <b>"],
    )

    rendered = render(template, language="en")

    assert "<script>" not in rendered.html
    assert "Hello &lt;script&gt;alert(1)&lt;/script&gt; &amp; welcome" in rendered.html
    assert "<li>Terms &lt;b&gt;</li>" in rendered.html
    assert "<table" in rendered.html and "<td" in rendered.html


def test_the_buyers_code_and_the_mpn_get_their_own_columns() -> None:
    own = replace(THERMISTOR, part_number="TH-NTC-10K")
    same = replace(THERMISTOR, part_number="NCP18XH103F03RB")
    only_own = replace(THERMISTOR, part_number="TH-NTC-10K", mpn=None)

    def columns(line: RenderLine) -> dict[str, str]:
        rows = table_rows(render(STANDARD_TEMPLATE, language="en", lines=(line,), due=None).text)
        return dict(zip(rows[0], rows[1], strict=True))

    assert columns(own)["Part no."] == "TH-NTC-10K"
    assert columns(own)["MPN"] == "NCP18XH103F03RB"
    # The same code twice is the buyer's code: no MPN column.
    assert columns(same)["Part no."] == "NCP18XH103F03RB" and "MPN" not in columns(same)
    # The buyer's code is never presented as a manufacturer part number.
    assert columns(only_own).get("Part no.") == "TH-NTC-10K" and "MPN" not in columns(only_own)
    assert columns(THERMISTOR)["MPN"] == "NCP18XH103F03RB"
    assert "Part no." not in columns(THERMISTOR)


# --- RFQ-3 (2026-10-08): steel, fasteners and paint from an MRP, to Brazilian suppliers

STEEL = RenderLine(
    position=1,
    description="AÇO CARBONO SAE 1020 CHAPA 3MM",
    part_number="100000101",
    mpn=None,
    manufacturer=None,
    quantities=[Decimal(7500)],
    unit="kg",
    need_by=date(2026, 10, 31),
    incoterm="DAP",
    delivery_location="Vila Solana 732",
    qualification=None,
)
PAINT = replace(
    STEEL,
    position=2,
    description="TINTA EPOXI CINZA RAL 7035",
    part_number="100000112",
    quantities=[Decimal(1850)],
    unit="l",
)


def test_an_english_translation_of_prazo_is_still_the_need_by_date() -> None:
    # The email writer translated the standard template's "Prazo" to "Lead time":
    # the column holds our need-by dates, so it is headed as such.
    display = replace(
        STANDARD_TEMPLATE, language="en", columns=["Item", "Qty.", "Unit", "Lead time"]
    )

    rows = table_rows(
        render(STANDARD_TEMPLATE, language="en", display=display, lines=(STEEL,)).text
    )

    assert "Lead time" not in rows[0]
    assert dict(zip(rows[0], rows[1], strict=True))["Need-by date"] == "2026-10-31"


def test_the_unit_is_the_lines_own() -> None:
    rows = table_rows(render(STANDARD_TEMPLATE, language="pt", lines=(STEEL, PAINT)).text)
    units = [dict(zip(rows[0], r, strict=True))["Un."] for r in rows[1:]]

    assert units == ["kg", "l"]


def test_no_tier_wording_without_tiers_and_tiers_when_there_are() -> None:
    single = render(STANDARD_TEMPLATE, language="en", lines=(STEEL,)).text
    tiered = render(STANDARD_TEMPLATE, language="en", lines=(THERMISTOR,)).text

    assert "quantity tier" not in single
    assert "Please quote the unit price, with the currency." in single
    assert "Please quote the unit price for each quantity tier, with the currency." in tiered


def test_rohs_and_reach_only_for_electronic_parts() -> None:
    materials = render(STANDARD_TEMPLATE, language="pt", lines=(STEEL, PAINT)).text
    electronics = render(STANDARD_TEMPLATE, language="pt", lines=(CAPACITOR,)).text

    assert "RoHS" not in materials and "REACH" not in materials
    assert "Declarações RoHS e REACH" in electronics


def test_the_signature_is_the_buyer_then_the_company_never_a_made_up_department() -> None:
    def closing(name: str) -> str:
        return render_rfq(
            RenderInput(
                rfq_ref="RFQ-3",
                rfq_title="Necessidades de materiais BR01",
                response_due=None,
                buyer_name=name,
                buyer_company="Biduuuuu",
                language="pt",
                template=STANDARD_TEMPLATE,
                display=STANDARD_TEMPLATE,
                lines=(STEEL,),
            )
        ).text

    assert closing("Ana Lima").endswith("Atenciosamente,\nAna Lima\nBiduuuuu\n")
    # No buyer name: the company alone (not "Biduuuuu purchasing").
    assert closing("Biduuuuu").endswith("Atenciosamente,\nBiduuuuu\n")
    assert "purchasing" not in closing("Biduuuuu")


def test_the_html_is_a_document_with_a_real_table() -> None:
    rendered = render(STANDARD_TEMPLATE, language="pt", lines=(STEEL, PAINT))

    assert rendered.html.startswith('<!doctype html><html lang="pt">')
    assert rendered.html.count("<tr>") == 3  # heading + two lines
    assert ">7500</td>" in rendered.html and "text-align:right" in rendered.html


def test_the_buyers_terms_are_stated_only_when_set() -> None:
    plain = render(BUYER_TEMPLATE)
    termed = render(BUYER_TEMPLATE, payment_terms="28 DDL", quote_validity_days=30)
    english = render(STANDARD_TEMPLATE, language="en", payment_terms="30 days net")

    payment = "Nossa condição de pagamento: 28 DDL"
    validity = "Por favor, mantenha a proposta válida por no mínimo 30 dias."
    for rendered in (termed.text, termed.html):
        assert payment in rendered and validity in rendered
    # After the questions and the alternative-part note, before the due date.
    paragraphs = termed.text.split("\n\n")
    at = paragraphs.index(f"{payment}\n{validity}")
    assert paragraphs[at - 1].startswith("Se oferecer um item alternativo")
    assert paragraphs[at + 1] == "Por favor, envie sua proposta até 20/10/2026."
    # Unset: no statement at all; the footer's own question to the supplier stays.
    assert "Nossa condição de pagamento" not in plain.text
    assert "válida por no mínimo" not in plain.text
    assert "- Condição de pagamento" in plain.text and "- Condição de pagamento" in termed.text
    assert "Our payment terms: 30 days net" in english.text
    assert "Please keep your quote valid" not in english.text


def test_the_email_checks_require_the_payment_terms_when_set() -> None:
    task = RfqEmailInput(
        rfq_ref="RFQ-7",
        rfq_title="BMS board rev C passives",
        buyer_company="Voltwerk Ltda",
        buyer_name="Carla Souza",
        supplier_name="Rhein",
        supplier_language="pt",
        lines=[],
        payment_terms="28 DDL",
    )
    with_terms = render(BUYER_TEMPLATE, payment_terms="28 DDL", quote_validity_days=30)
    without = render(BUYER_TEMPLATE)

    def problems(rendered: RenderedRfq) -> list[str]:
        return rfq_email_problems(
            task, RfqEmailOutput(subject=rendered.subject, body=rendered.text)
        )

    assert problems(with_terms) == []
    assert problems(without) == ["payment_terms_missing"]


def test_a_bido_column_empty_on_every_line_is_dropped_supplier_columns_stay() -> None:
    template = replace(
        BUYER_TEMPLATE, columns=["Descrição", "Qtd", "Un", "Prazo", "Preço unitário"]
    )
    no_dates = tuple(replace(line, need_by=None) for line in (THERMISTOR, CAPACITOR))

    header, *rows = table_rows(render(template, lines=no_dates).text)
    assert "Prazo" not in header  # our need-by column, empty on every line
    assert "Preço unitário" in header  # the supplier fills it: stays empty on purpose
    assert all(len(row) == len(header) for row in rows)

    header, *_ = table_rows(render(template).text)
    assert "Prazo" in header  # with dates it stays


def cover(language: str = "pt", **overrides: object) -> RenderedRfq:
    data = RenderInput(
        rfq_ref="RFQ-7",
        rfq_title="BMS board rev C passives",
        response_due=date(2026, 10, 20),
        buyer_name="Carla Souza",
        buyer_company="Voltwerk Ltda",
        language=language,
        template=BUYER_TEMPLATE,
        display=BUYER_TEMPLATE,
        lines=(THERMISTOR, CAPACITOR),
    )
    return render_cover(replace(data, **overrides))  # pyright: ignore[reportArgumentType]


def test_the_cover_note_says_what_is_attached_by_when_and_how_to_reply() -> None:
    pt = cover()
    assert pt.subject == "[RFQ-7] Solicitação de cotação: BMS board rev C passives"
    assert pt.text == (
        "Prezados,\n\n"
        "Segue em anexo nossa solicitação de cotação RFQ-7 (BMS board rev C passives). "
        "A solicitação tem 2 itens; as quantidades, as condições e o que informar na "
        "proposta estão no PDF anexo.\n\n"
        "Por favor, envie sua proposta até 20/10/2026.\n\n"
        "Para responder, responda a este e-mail mantendo [RFQ-7] no assunto e anexe "
        "sua proposta.\n\n"
        "Atenciosamente,\nCarla Souza\nVoltwerk Ltda\n"
    )
    # No item table, no questions, no buyer greeting: those are in the PDF.
    for absent in ("NCP18XH103F03RB", "10000", "Prazo de entrega", "Prezados, boa tarde"):
        assert absent not in pt.text and absent not in pt.html
    assert "<table" not in pt.html and "[RFQ-7]" in pt.html

    en = cover("en", lines=(CAPACITOR,), response_due=None)
    assert "It covers 1 item;" in en.text and "Please send your quote" not in en.text
    assert "keeping [RFQ-7] in the subject" in en.text


def test_every_language_has_the_cover_phrases_of_english() -> None:
    from bido_back.templates.render import _COVER, _PHRASES  # pyright: ignore[reportPrivateUsage]

    assert set(_COVER) == set(_PHRASES)
    for language, words in _COVER.items():
        assert set(words) == set(_COVER["en"]), language
        text = cover(language).text
        assert "RFQ-7" in text and "[RFQ-7]" in text and "2" in text


def test_the_cover_passes_its_checks_and_a_listed_item_does_not() -> None:
    task = RfqEmailInput(
        rfq_ref="RFQ-7",
        rfq_title="BMS board rev C passives",
        buyer_company="Voltwerk Ltda",
        buyer_name="Carla Souza",
        supplier_name="Rhein",
        supplier_language="de",
        lines=[
            RfqEmailLine(
                position=1,
                description="x",
                mpn="NCP18XH103F03RB",
                quantities=[Decimal(1)],
                unit="pcs",
            )
        ],
    )
    good = cover("de")
    assert rfq_cover_problems(task, RfqEmailOutput(subject=good.subject, body=good.text)) == []
    listed = RfqEmailOutput(subject=good.subject, body=good.text + "NCP18XH103F03RB 0,05 EUR")
    assert rfq_cover_problems(task, listed) == ["items_listed", "price_mentioned"]
    unmarked = RfqEmailOutput(subject="Angebot", body=good.text.replace("[RFQ-7]", "RFQ-7"))
    assert rfq_cover_problems(task, unmarked) == [
        "rfq_ref_missing_in_subject",
        "reply_marker_missing",
    ]
