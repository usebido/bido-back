"""The RFQ email render: deterministic, from the template and the RFQ lines."""

from datetime import date
from decimal import Decimal

from bido_back.agents.catalog.contracts import RfqEmailInput, RfqEmailLine, RfqEmailOutput
from bido_back.agents.catalog.definitions import rfq_email_problems
from bido_back.onboarding.standard import STANDARD_TEMPLATE, TemplateContent
from bido_back.templates.render import (
    RenderedRfq,
    RenderInput,
    RenderLine,
    column_key,
    render_rfq,
)

THERMISTOR = RenderLine(
    position=1,
    description="NTC thermistor 10k",
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
    assert table_rows(rendered.text)[0][:3] == ["Pos.", "Pos. und Bezeichnung", "Menge"]
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
