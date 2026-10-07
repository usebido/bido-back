"""Bido's default compliance requirement set for EV electronics suppliers.

Used while a tenant has stored none. Blocking: the declarations a buyer cannot
ship without (RoHS, REACH) and the AEC-Q report for lines that ask for it.
The rest are tracked but do not block a supplier until the tenant says so.
"""

from typing import Final

from bido_back.compliance.schemas import Requirement

DEFAULT_REQUIREMENTS: Final[tuple[Requirement, ...]] = (
    Requirement(
        code="rohs",
        name="RoHS declaration",
        scope="supplier",
        keywords=["rohs"],
        validity_months=12,
        blocking=True,
    ),
    Requirement(
        code="reach_svhc",
        name="REACH SVHC declaration",
        scope="supplier",
        keywords=["reach", "svhc"],
        validity_months=12,
        blocking=True,
    ),
    Requirement(
        code="cmrt",
        name="Conflict minerals report (CMRT)",
        scope="supplier",
        keywords=["cmrt", "conflict mineral", "conflict_mineral", "3tg"],
        validity_months=12,
    ),
    Requirement(
        code="aec_q",
        name="AEC-Q100/Q200 qualification report",
        scope="line",
        line_keyword="aec-q",
        keywords=["aec-q", "aecq", "aec_q", "qualification report", "qual report"],
        blocking=True,
    ),
    Requirement(
        code="iatf_16949",
        name="IATF 16949 certificate",
        scope="manufacturer",
        keywords=["iatf", "16949"],
    ),
    Requirement(
        code="iso_9001",
        name="ISO 9001 certificate",
        scope="supplier",
        keywords=["iso 9001", "iso9001", "iso_9001", "iso-9001"],
    ),
    Requirement(
        code="ppap",
        name="PPAP submission (when requested)",
        scope="line",
        line_keyword="ppap",
        keywords=["ppap", "psw"],
    ),
)
