"""The milestones a PO payment's escrow is released by, and what the factory sends for each.

The tooling plan, in order, each milestone released through its own gate (gate = index),
so each release is one transaction:

| key | on chain | share | released when | the factory must send |
|---|---|---|---|---|
| `tool_start` | Tool start | 30% | design frozen, steel committed | steel photos, certificate |
| `t1` | T1 | 30% | the tool runs, samples measured | T1 report, sample photos |
| `ppap` | PPAP | 40% | proven at scale (PSW, run) | PSW, dimensions, run at rate |

Each milestone also takes optional evidence (DFM, mold design, schedule; trial video, mold
photos, issue list, waybill; capability, material, deviations, photos). T2/T3 re-trials are new
attempts at T1, never a new milestone. The shares are the default; a PO may use others
(e.g. 30/40/30, common with Chinese mold makers) through the milestones' bps. A payment's
milestones carry the catalog `key`; a custom milestone (no key) asks for one piece of
evidence. Pure: no I/O and no solders, so the service can fix a payment's terms with it.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Final

BPS_TOTAL: Final = 10_000
PDF: Final = ("pdf",)
IMAGES: Final = ("jpg", "jpeg", "png", "webp")
PDF_OR_IMAGES: Final = ("pdf", *IMAGES)
REPORTS: Final = ("pdf", "xlsx")
VIDEOS: Final = ("mp4", "mov")


@dataclass(frozen=True, slots=True)
class DocumentType:
    code: str
    label: str
    label_zh: str
    required: bool
    accepts: tuple[str, ...]
    description: str


@dataclass(frozen=True, slots=True)
class MilestoneDefinition:
    key: str | None
    name: str  # on chain: 1..24 UTF-8 bytes
    label: str
    label_zh: str
    bps: int
    description: str
    documents: tuple[DocumentType, ...]
    checks: tuple[str, ...]

    def document(self, code: str) -> DocumentType | None:
        return next((d for d in self.documents if d.code == code), None)

    @property
    def required_codes(self) -> frozenset[str]:
        return frozenset(d.code for d in self.documents if d.required)


# --- The tooling plan -------------------------------------------------------------------------

TOOL_START: Final = MilestoneDefinition(
    key="tool_start",
    name="Tool start",
    label="Tool start",
    label_zh="模具启动",
    bps=3000,
    description="The design is frozen and the tool steel is at the factory, committed to this "
    "tool.",
    documents=(
        DocumentType(
            code="tool_start_photo",
            label="Photos of the tool steel, marked with the tool ID and PO number",
            label_zh="钢材到厂照片（标明模具编号和订单号）",
            required=True,
            accepts=IMAGES,
            description="The cavity and core steel at the factory, the steel maker's stamp "
            "visible. Several photos allowed.",
        ),
        DocumentType(
            code="steel_certificate",
            label="Steel certificate",
            label_zh="钢材材质证明（质保书）",
            required=True,
            accepts=PDF_OR_IMAGES,
            description="The mill certificate of the cavity and core steel: grade, steel maker, "
            "heat number, sizes.",
        ),
        DocumentType(
            code="dfm_report",
            label="Approved DFM report",
            label_zh="DFM 报告（可制造性分析）",
            required=False,
            accepts=PDF,
            description="The final design-for-manufacturing review the buyer agreed.",
        ),
        DocumentType(
            code="mold_design",
            label="Mold design (3D/2D)",
            label_zh="模具设计图（3D/2D）",
            required=False,
            accepts=PDF_OR_IMAGES,
            description="The final mold design and general assembly.",
        ),
        DocumentType(
            code="tooling_schedule",
            label="Tooling schedule",
            label_zh="模具进度计划表",
            required=False,
            accepts=(*PDF_OR_IMAGES, "xlsx"),
            description="Planned dates for steel, machining, T0, T1, T2 and PPAP.",
        ),
    ),
    checks=(
        "The DFM is closed and the mold design matches the PO's part and revision.",
        "The steel certificate's grade and steel maker match the quote.",
        "The photographed steel is marked for this tool and matches the certificate.",
    ),
)

T1: Final = MilestoneDefinition(
    key="t1",
    name="T1",
    label="T1 samples",
    label_zh="T1 试模样件",
    bps=3000,
    description="The tool is complete and running: first samples molded on it and measured, "
    "with every deviation on a correction plan.",
    documents=(
        DocumentType(
            code="t1_report",
            label="T1 trial report with the dimensional results",
            label_zh="T1 试模报告（含尺寸检测）",
            required=True,
            accepts=REPORTS,
            description="Trial date, press, material and process parameters, and the samples "
            "measured against the drawing, per cavity, with the out-of-tolerance items marked.",
        ),
        DocumentType(
            code="t1_photo",
            label="T1 sample photos",
            label_zh="T1 样件照片（可上传多张）",
            required=True,
            accepts=IMAGES,
            description="Each cavity's samples from several angles, defects in close-up.",
        ),
        DocumentType(
            code="t1_video",
            label="Trial video",
            label_zh="试模视频（连续 2–3 模，拍到铭牌）",
            required=False,
            accepts=VIDEOS,
            description="Two or three consecutive cycles, the tool's nameplate in view.",
        ),
        DocumentType(
            code="t1_mold_photo",
            label="Mold photos (open, closed, nameplate)",
            label_zh="模具照片（开模、合模、铭牌）",
            required=False,
            accepts=IMAGES,
            description="Both halves open, the tool closed, and its nameplate.",
        ),
        DocumentType(
            code="t1_issue_list",
            label="Issue list and correction plan",
            label_zh="问题点清单及修模计划",
            required=False,
            accepts=REPORTS,
            description="Each open issue with its cause, the correction and the T2 date.",
        ),
        DocumentType(
            code="t1_shipment",
            label="Sample shipment waybill",
            label_zh="寄样快递单",
            required=False,
            accepts=PDF_OR_IMAGES,
            description="The carrier and tracking number of the T1 samples sent to the buyer.",
        ),
    ),
    checks=(
        "The samples come from this tool: nameplate and design as approved at Tool start.",
        "Every cavity, slide and lifter works and the tool runs.",
        "The dimensional results cover the drawing's critical characteristics.",
        "Every deviation is on the correction plan with a date, or is an agreed steel-safe "
        "dimension.",
    ),
)

PPAP: Final = MilestoneDefinition(
    key="ppap",
    name="PPAP",
    label="PPAP approval",
    label_zh="PPAP 批准",
    bps=4000,
    description="Proven at scale: a production run on this tool, at the quoted rate, made "
    "parts that meet every requirement (AIAG PPAP, the factory's signed warrant).",
    documents=(
        DocumentType(
            code="ppap_psw",
            label="Part Submission Warrant (PSW), signed",
            label_zh="零件提交保证书 PSW（PDF）",
            required=True,
            accepts=PDF,
            description="The factory's signed declaration that the production parts meet every "
            "requirement of the drawing and the order.",
        ),
        DocumentType(
            code="ppap_dimensional_report",
            label="Full dimensional report, every cavity",
            label_zh="全尺寸检测报告（每穴）",
            required=True,
            accepts=REPORTS,
            description="Every characteristic of the drawing measured on parts from the "
            "production run, for every cavity.",
        ),
        DocumentType(
            code="ppap_run_at_rate",
            label="Run-at-rate report",
            label_zh="节拍生产（Run@Rate）报告",
            required=True,
            accepts=(*REPORTS, *IMAGES),
            description="The production run on this tool: at least 300 consecutive parts, its "
            "duration, cavities running, good parts per hour against the quoted rate.",
        ),
        DocumentType(
            code="ppap_capability_study",
            label="Process capability study (Ppk)",
            label_zh="初始过程研究（过程能力 Ppk）",
            required=False,
            accepts=REPORTS,
            description="Ppk of the special characteristics, from the production run.",
        ),
        DocumentType(
            code="ppap_material_cert",
            label="Material certificate",
            label_zh="材质证明（原材料质保书）",
            required=False,
            accepts=PDF_OR_IMAGES,
            description="The part material's certificate: grade and lot used in the run.",
        ),
        DocumentType(
            code="ppap_deviation_request",
            label="Deviation request and action plan",
            label_zh="偏差（让步）申请及整改计划",
            required=False,
            accepts=REPORTS,
            description="Every characteristic out of specification, its cause, actions and dates.",
        ),
        DocumentType(
            code="ppap_photo",
            label="Photos of parts from every cavity and the tool's nameplate",
            label_zh="样件照片（按穴号）及模具铭牌",
            required=False,
            accepts=IMAGES,
            description="Parts from the production run, cavity numbers visible, and the tool tag.",
        ),
    ),
    checks=(
        "The PSW is signed, names the part number and drawing revision on the order, and "
        "declares that the results meet all requirements.",
        "The dimensional report covers every characteristic for every cavity, each within "
        "tolerance or covered by an accepted deviation.",
        "The run shows at least 300 consecutive parts on this tool at the quoted rate.",
        "Special characteristics reach Ppk 1.67 (1.33 to 1.67 needs your review).",
        "An interim approval is not an approval: ask for changes (reason `ppap_interim_only`).",
    ),
)

TOOLING_PLAN: Final = (TOOL_START, T1, PPAP)
CATALOG: Final[dict[str, MilestoneDefinition]] = {m.key: m for m in TOOLING_PLAN if m.key}

# A custom milestone (not from the catalog) asks for one piece of evidence.
EVIDENCE: Final = DocumentType(
    code="evidence",
    label="Evidence the milestone is reached",
    label_zh="完成证明",
    required=True,
    accepts=(*PDF_OR_IMAGES, "xlsx"),
    description="A document or photo showing the milestone is done.",
)


def definition_for(key: str | None, name: str, bps: int) -> MilestoneDefinition:
    """The catalog's definition of a milestone, or a generic one for a custom milestone."""
    if key is not None and key in CATALOG:
        found = CATALOG[key]
        return MilestoneDefinition(
            key=found.key,
            name=found.name,
            label=found.label,
            label_zh=found.label_zh,
            bps=bps,
            description=found.description,
            documents=found.documents,
            checks=found.checks,
        )
    return MilestoneDefinition(
        key=None,
        name=name,
        label=name,
        label_zh=name,
        bps=bps,
        description=f"The milestone {name} is reached.",
        documents=(EVIDENCE,),
        checks=("The evidence shows the milestone is reached.",),
    )


def milestone_amounts(total: int, bps: Sequence[int]) -> list[int]:
    """The program's split (`create_po` check 7): every milestone but the last gets
    `floor(total × bps / 10_000)`, the last the rest, so they add up to `total`."""
    if not bps:
        return []
    leading = [total * share // BPS_TOTAL for share in bps[:-1]]
    return [*leading, total - sum(leading)]


def decimal_shares(total: Decimal, bps: Sequence[int], places: int = 2) -> list[Decimal]:
    """The same split of an amount in a currency (display only): rounded down to `places`,
    the last milestone takes the rest."""
    quantum = Decimal(1).scaleb(-places)
    leading = [
        (total * share / BPS_TOTAL).quantize(quantum, rounding=ROUND_DOWN) for share in bps[:-1]
    ]
    return [*leading, total - sum(leading, Decimal(0))] if bps else []
