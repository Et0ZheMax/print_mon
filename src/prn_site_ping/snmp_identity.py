from __future__ import annotations

from dataclasses import dataclass, field
import re


SYS_DESCR_OID = "1.3.6.1.2.1.1.1.0"
SYS_OBJECT_ID_OID = "1.3.6.1.2.1.1.2.0"
PRT_GENERAL_PRINTER_NAME_OID = "1.3.6.1.2.1.43.5.1.1.16.1"
HR_DEVICE_DESCR_OID = "1.3.6.1.2.1.25.3.2.1.3.1"


@dataclass(frozen=True)
class PrinterIdentity:
    vendor: str | None
    model: str | None
    family: str | None
    sysdescr: str | None
    sysobjectid: str | None
    raw_identifiers: dict[str, str] = field(default_factory=dict)


def detect_printer_identity(values: dict[str, str]) -> PrinterIdentity:
    sysdescr = values.get(SYS_DESCR_OID)
    sysobjectid = values.get(SYS_OBJECT_ID_OID)
    printer_name = values.get(PRT_GENERAL_PRINTER_NAME_OID)
    hr_descr = values.get(HR_DEVICE_DESCR_OID)

    raw = {k: v for k, v in values.items() if v}
    merged = " | ".join(part for part in (sysdescr, printer_name, hr_descr) if part)
    lowered = merged.casefold()

    vendor = _detect_vendor(lowered, sysobjectid)
    model = _detect_model(merged, vendor)
    family = _detect_family(model, merged, vendor)

    return PrinterIdentity(
        vendor=vendor,
        model=model,
        family=family,
        sysdescr=sysdescr,
        sysobjectid=sysobjectid,
        raw_identifiers=raw,
    )


def has_identity(identity: PrinterIdentity) -> bool:
    return bool(identity.vendor or identity.model or identity.sysdescr or identity.sysobjectid)


def _detect_vendor(lowered_text: str, sysobjectid: str | None) -> str | None:
    if "kyocera" in lowered_text or "ecosys" in lowered_text:
        return "kyocera"
    if "godex" in lowered_text:
        return "godex"

    if not sysobjectid:
        return None
    if sysobjectid.startswith("1.3.6.1.4.1.1347"):
        return "kyocera"
    if sysobjectid.startswith("1.3.6.1.4.1.2674"):
        return "godex"
    return None


def _detect_model(text: str, vendor: str | None) -> str | None:
    if not text:
        return None

    if vendor == "kyocera":
        m = re.search(r"\b(?:ECOSYS\s+)?([MP]?\d{4,5}[a-z]{2,6})\b", text, flags=re.IGNORECASE)
        if m:
            return m.group(1).upper()

    if vendor == "godex":
        m = re.search(r"\b(G\w{2,10})\b", text, flags=re.IGNORECASE)
        if m:
            return m.group(1).upper()

    generic = re.search(r"\b([A-Z]{1,4}\d{3,5}[A-Z0-9-]{0,6})\b", text)
    return generic.group(1) if generic else None


def _detect_family(model: str | None, text: str, vendor: str | None) -> str | None:
    lowered = text.casefold()
    if vendor == "kyocera":
        if "ecosys" in lowered:
            return "ecosys"
        if model and model.startswith(("M", "P")):
            return "ecosys"
        return "kyocera_generic"
    if vendor == "godex":
        return "godex_label"
    return None
