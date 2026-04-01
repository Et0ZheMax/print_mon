from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Protocol

from .models import CardSeverity, SupplyLevel
from .snmp_identity import PrinterIdentity

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdapterFetchResult:
    supplies: tuple[SupplyLevel, ...] = ()
    reason: str | None = None
    partial: bool = False


class SnmpAdapterOps(Protocol):
    def walk(self, base_oid: str) -> dict[str, str]: ...


class VendorSuppliesAdapter(Protocol):
    name: str

    def matches(self, identity: PrinterIdentity) -> bool: ...

    def fetch_supplies(self, ops: SnmpAdapterOps, identity: PrinterIdentity) -> AdapterFetchResult: ...


class KyoceraEcosysAdapter:
    name = "kyocera_adapter"
    ENTERPRISE_OID = "1.3.6.1.4.1.1347"

    # В парке встречаются разные ветки прошивок/серий, поэтому пробуем несколько
    # типовых enterprise-подветок Kyocera.
    CANDIDATE_BASES = (
        f"{ENTERPRISE_OID}.43.5.4.1",
        f"{ENTERPRISE_OID}.43.10.2.1",
        f"{ENTERPRISE_OID}.43.18.1",
    )

    def matches(self, identity: PrinterIdentity) -> bool:
        return identity.vendor == "kyocera" or identity.family == "ecosys"

    def fetch_supplies(self, ops: SnmpAdapterOps, identity: PrinterIdentity) -> AdapterFetchResult:
        parsed: list[SupplyLevel] = []
        parse_errors = 0

        for base in self.CANDIDATE_BASES:
            data = ops.walk(base)
            if not data:
                continue
            for oid, raw in data.items():
                supply = _parse_percentish_supply(raw, oid, source=self.name)
                if supply:
                    parsed.append(supply)
                elif _might_be_supply_value(raw):
                    parse_errors += 1

        deduped = _dedupe_supplies(parsed)
        if deduped:
            partial = any(item.percent is None for item in deduped)
            return AdapterFetchResult(supplies=tuple(sorted(deduped, key=_sort_key)), partial=partial)

        if parse_errors:
            return AdapterFetchResult(reason="kyocera adapter found no toner values")
        return AdapterFetchResult(reason="kyocera adapter found no known oids")


class GodexAdapter:
    name = "godex_adapter"
    ENTERPRISE_OID = "1.3.6.1.4.1.2674"

    def matches(self, identity: PrinterIdentity) -> bool:
        return identity.vendor == "godex" or identity.family == "godex_label"

    def fetch_supplies(self, ops: SnmpAdapterOps, identity: PrinterIdentity) -> AdapterFetchResult:
        data = ops.walk(self.ENTERPRISE_OID)
        if not data:
            return AdapterFetchResult(reason="godex adapter found no telemetry")

        parsed: list[SupplyLevel] = []
        for oid, raw in data.items():
            supply = _parse_percentish_supply(raw, oid, source=self.name)
            if supply:
                parsed.append(supply)

        if not parsed:
            return AdapterFetchResult(reason="godex adapter found no supply values")
        return AdapterFetchResult(supplies=tuple(sorted(_dedupe_supplies(parsed), key=_sort_key)))


class AdapterRegistry:
    def __init__(self, adapters: list[VendorSuppliesAdapter] | None = None):
        self._adapters = adapters or [KyoceraEcosysAdapter(), GodexAdapter()]

    def match(self, identity: PrinterIdentity) -> VendorSuppliesAdapter | None:
        for adapter in self._adapters:
            if adapter.matches(identity):
                LOGGER.info("Adapter matched: %s (vendor=%s family=%s model=%s)", adapter.name, identity.vendor, identity.family, identity.model)
                return adapter
        return None


def _might_be_supply_value(raw: str) -> bool:
    token = raw.casefold()
    return any(part in token for part in ("toner", "ink", "%", "black", "cyan", "magenta", "yellow", "k:", "c:", "m:", "y:"))


def _parse_percentish_supply(raw: str, oid: str, source: str) -> SupplyLevel | None:
    text = str(raw).strip()
    if not text or not _might_be_supply_value(text):
        return None

    color = _detect_color(text)
    percent = _extract_percent(text)
    if percent is None:
        return None

    name = color or _guess_name_from_oid(oid)
    return SupplyLevel(
        name=name,
        kind="toner",
        color=color,
        percent=percent,
        severity=_supply_severity(percent),
        source=source,
    )


def _extract_percent(text: str) -> int | None:
    m = re.search(r"(\d{1,3})\s*%", text)
    if m:
        return max(0, min(100, int(m.group(1))))

    m2 = re.search(r"\b([kcm y])\s*[:=]\s*(\d{1,3})\b", text.casefold())
    if m2:
        return max(0, min(100, int(m2.group(2))))

    pure = re.fullmatch(r"\d{1,3}", text)
    if pure:
        return max(0, min(100, int(pure.group(0))))
    return None


def _detect_color(text: str) -> str | None:
    lower = text.casefold()
    mapping = {
        "black": "K",
        "cyan": "C",
        "magenta": "M",
        "yellow": "Y",
        " k ": "K",
        " c ": "C",
        " m ": "M",
        " y ": "Y",
    }
    padded = f" {lower} "
    for token, value in mapping.items():
        if token in padded:
            return value
    m = re.search(r"\b([kcmy])\s*[:=]", lower)
    return m.group(1).upper() if m else None


def _guess_name_from_oid(oid: str) -> str:
    return oid.split(".")[-1]


def _supply_severity(percent: int | None) -> CardSeverity:
    if percent is None:
        return CardSeverity.UNKNOWN
    if percent < 10:
        return CardSeverity.CRITICAL
    if percent < 20:
        return CardSeverity.WARNING
    return CardSeverity.OK


def _dedupe_supplies(supplies: list[SupplyLevel]) -> list[SupplyLevel]:
    best: dict[tuple[str, str], SupplyLevel] = {}
    for item in supplies:
        key = (item.kind, item.color or item.name)
        previous = best.get(key)
        if not previous or (item.percent or -1) >= (previous.percent or -1):
            best[key] = item
    return list(best.values())


def _sort_key(item: SupplyLevel) -> tuple[int, int, str]:
    color_order = {"K": 0, "C": 1, "M": 2, "Y": 3}
    is_color = 0 if item.color in color_order else 1
    return is_color, color_order.get(item.color or "", 99), item.name
