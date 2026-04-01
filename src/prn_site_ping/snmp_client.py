from __future__ import annotations

import logging
import re
from collections import defaultdict

from .models import CardSeverity, SnmpConfig, SnmpTelemetryResult, SupplyLevel
from .snmp_adapters import AdapterRegistry
from .snmp_identity import (
    HR_DEVICE_DESCR_OID,
    PRT_GENERAL_PRINTER_NAME_OID,
    SYS_DESCR_OID,
    SYS_OBJECT_ID_OID,
    detect_printer_identity,
    has_identity,
)

LOGGER = logging.getLogger(__name__)

SUPPLIES_TABLE_BASE = "1.3.6.1.2.1.43.11.1.1"
SUPPLY_FIELDS = {
    "4": "colorant",
    "5": "class",
    "6": "desc",
    "7": "unit",
    "8": "max",
    "9": "level",
}

IDENTITY_OIDS = (SYS_DESCR_OID, SYS_OBJECT_ID_OID, PRT_GENERAL_PRINTER_NAME_OID, HR_DEVICE_DESCR_OID)

DIAG_SNMP_DISABLED = "SNMP disabled"
DIAG_SNMP_LIB_MISSING = "SNMP library missing"
DIAG_SNMP_TIMEOUT = "SNMP timeout"
DIAG_SNMP_AUTH = "SNMP auth/community failed"
DIAG_SNMP_NO_SUPPLIES = "standard printer mib supplies not available"
DIAG_SNMP_INVALID_DATA = "invalid supplies table data"
DIAG_SNMP_PARTIAL = "partial data only"
DIAG_ADAPTER_NOT_MATCHED = "standard supplies unavailable; no adapter matched"
DIAG_IDENTITY_NOT_DETECTED = "standard supplies unavailable; identity not detected"
DIAG_ADAPTER_FAILED = "standard supplies unavailable; vendor adapter failed"

COLOR_BY_TOKEN = {
    "black": "K",
    "cyan": "C",
    "magenta": "M",
    "yellow": "Y",
    "k": "K",
    "c": "C",
    "m": "M",
    "y": "Y",
}


def _parse_supplies_table_oid(oid: str) -> tuple[str, str] | None:
    if not oid.startswith(SUPPLIES_TABLE_BASE + "."):
        return None
    rest = oid[len(SUPPLIES_TABLE_BASE) + 1 :]
    parts = rest.split(".")
    if len(parts) < 3:
        return None

    field_id = parts[0]
    row_index = ".".join(parts[1:])
    if not row_index:
        return None
    return field_id, row_index


def _import_pysnmp() -> tuple[object | None, str | None]:
    try:
        from pysnmp.hlapi import (  # type: ignore
            CommunityData,
            ContextData,
            ObjectIdentity,
            ObjectType,
            SnmpEngine,
            UdpTransportTarget,
            getCmd,
            nextCmd,
        )
    except Exception:
        return None, DIAG_SNMP_LIB_MISSING
    return {
        "CommunityData": CommunityData,
        "ContextData": ContextData,
        "ObjectIdentity": ObjectIdentity,
        "ObjectType": ObjectType,
        "SnmpEngine": SnmpEngine,
        "UdpTransportTarget": UdpTransportTarget,
        "getCmd": getCmd,
        "nextCmd": nextCmd,
    }, None


class SnmpClient:
    def __init__(self, cfg: SnmpConfig):
        self.cfg = cfg
        self.adapter_registry = AdapterRegistry()

    def fetch_supplies(self, host: str) -> SnmpTelemetryResult:
        if not self.cfg.enabled:
            return SnmpTelemetryResult(ok=False, reason=DIAG_SNMP_DISABLED)

        api, import_error = _import_pysnmp()
        if import_error:
            LOGGER.warning("SNMP library unavailable: pysnmp not installed")
            return SnmpTelemetryResult(ok=False, reason=import_error)

        standard = self._fetch_standard_supplies(host, api)
        if standard.supplies:
            return SnmpTelemetryResult(ok=True, supplies=standard.supplies, partial=standard.partial, reason=standard.reason, source="standard_printer_mib")

        identity_map = self._get_oids(host, api, IDENTITY_OIDS)
        identity = detect_printer_identity(identity_map)
        LOGGER.info(
            "Identity discovery for %s: vendor=%s family=%s model=%s sysObjectID=%s",
            host,
            identity.vendor,
            identity.family,
            identity.model,
            identity.sysobjectid,
        )

        if not has_identity(identity):
            return SnmpTelemetryResult(ok=standard.ok, supplies=(), reason=DIAG_IDENTITY_NOT_DETECTED, source="standard_printer_mib")

        adapter = self.adapter_registry.match(identity)
        if not adapter:
            return SnmpTelemetryResult(ok=standard.ok, supplies=(), reason=DIAG_ADAPTER_NOT_MATCHED, source="standard_printer_mib")

        try:
            adapter_result = adapter.fetch_supplies(_AdapterOps(self, host, api), identity)
        except Exception as exc:
            LOGGER.exception("Vendor adapter %s failed for %s", adapter.name, host)
            return SnmpTelemetryResult(ok=standard.ok, supplies=(), reason=f"{DIAG_ADAPTER_FAILED}: {exc}", source=adapter.name)

        if adapter_result.supplies:
            LOGGER.info("Vendor adapter %s succeeded for %s with %s supplies", adapter.name, host, len(adapter_result.supplies))
            return SnmpTelemetryResult(
                ok=True,
                supplies=adapter_result.supplies,
                reason=adapter_result.reason,
                partial=adapter_result.partial,
                source=adapter.name,
            )

        adapter_reason = adapter_result.reason or DIAG_ADAPTER_FAILED
        return SnmpTelemetryResult(ok=standard.ok, supplies=(), reason=f"standard supplies unavailable; {adapter_reason}", partial=adapter_result.partial, source=adapter.name)

    def _fetch_standard_supplies(self, host: str, api) -> SnmpTelemetryResult:
        rows: dict[str, dict[str, str | int]] = defaultdict(dict)
        walk_result = self._walk(host, api, SUPPLIES_TABLE_BASE)
        if walk_result[1]:
            return SnmpTelemetryResult(ok=False, reason=walk_result[1])

        for oid, value in walk_result[0].items():
            parsed = _parse_supplies_table_oid(oid)
            if not parsed:
                continue
            field_id, idx = parsed
            key = SUPPLY_FIELDS.get(field_id)
            if key:
                rows[idx][key] = value

        supplies, partial, invalid_count = normalize_supply_rows(rows)
        if not supplies:
            reason = DIAG_SNMP_INVALID_DATA if invalid_count > 0 else DIAG_SNMP_NO_SUPPLIES
            return SnmpTelemetryResult(ok=True, supplies=(), reason=reason, partial=partial)
        if partial:
            return SnmpTelemetryResult(ok=True, supplies=tuple(supplies), reason=DIAG_SNMP_PARTIAL, partial=True)
        return SnmpTelemetryResult(ok=True, supplies=tuple(supplies), source="standard_printer_mib")

    def _walk(self, host: str, api, base_oid: str) -> tuple[dict[str, str], str | None]:
        values: dict[str, str] = {}
        try:
            iterator = api["nextCmd"](
                api["SnmpEngine"](),
                api["CommunityData"](self.cfg.community, mpModel=1),
                api["UdpTransportTarget"]((host, int(self.cfg.port)), timeout=float(self.cfg.timeout), retries=int(self.cfg.retries)),
                api["ContextData"](),
                api["ObjectType"](api["ObjectIdentity"](base_oid)),
                lexicographicMode=False,
            )
            for error_indication, error_status, error_index, var_binds in iterator:
                if error_indication:
                    return {}, _classify_snmp_error(str(error_indication))
                if error_status:
                    message = f"{error_status.prettyPrint()} at {error_index}"
                    return {}, _classify_snmp_error(message)
                for oid_obj, value_obj in var_binds:
                    values[oid_obj.prettyPrint()] = value_obj.prettyPrint()
        except Exception as exc:
            LOGGER.error("SNMP walk failed for %s (%s): %s", host, base_oid, exc)
            return {}, _classify_snmp_error(str(exc))
        return values, None

    def _get_oids(self, host: str, api, oids: tuple[str, ...]) -> dict[str, str]:
        values: dict[str, str] = {}
        for oid in oids:
            value, err = self._get_one(host, api, oid)
            if err:
                continue
            if value is not None:
                values[oid] = value
        return values

    def _get_one(self, host: str, api, oid: str) -> tuple[str | None, str | None]:
        try:
            iterator = api["getCmd"](
                api["SnmpEngine"](),
                api["CommunityData"](self.cfg.community, mpModel=1),
                api["UdpTransportTarget"]((host, int(self.cfg.port)), timeout=float(self.cfg.timeout), retries=int(self.cfg.retries)),
                api["ContextData"](),
                api["ObjectType"](api["ObjectIdentity"](oid)),
            )
            error_indication, error_status, error_index, var_binds = next(iterator)
            if error_indication:
                return None, _classify_snmp_error(str(error_indication))
            if error_status:
                message = f"{error_status.prettyPrint()} at {error_index}"
                return None, _classify_snmp_error(message)
            if not var_binds:
                return None, None
            return var_binds[0][1].prettyPrint(), None
        except Exception as exc:
            return None, _classify_snmp_error(str(exc))


class _AdapterOps:
    def __init__(self, client: SnmpClient, host: str, api):
        self._client = client
        self._host = host
        self._api = api

    def walk(self, base_oid: str) -> dict[str, str]:
        values, err = self._client._walk(self._host, self._api, base_oid)
        if err:
            LOGGER.info("Adapter walk failed for %s base=%s: %s", self._host, base_oid, err)
            return {}
        return values


def normalize_supply_rows(rows: dict[str, dict[str, str | int]]) -> tuple[list[SupplyLevel], bool, int]:
    supplies: list[SupplyLevel] = []
    partial = False
    invalid_count = 0

    for raw in rows.values():
        desc = str(raw.get("desc", "")).strip()
        if not _is_useful_supply(desc, raw.get("class")):
            continue

        kind = _detect_kind(desc)
        color = _detect_color(desc)
        name = _display_name(desc, color, kind)

        max_value = _to_int(raw.get("max"))
        level_value = _to_int(raw.get("level"))
        unit = _unit_name(raw.get("unit"))
        percent = _calc_percent(level_value, max_value)

        is_unknown = percent is None
        if is_unknown:
            partial = True
            if max_value is not None or level_value is not None:
                invalid_count += 1

        severity = _supply_severity(percent)
        supplies.append(
            SupplyLevel(
                name=name,
                kind=kind,
                color=color,
                percent=percent,
                level_raw=level_value,
                max_raw=max_value,
                unit=unit,
                severity=severity,
                is_unknown=is_unknown,
            )
        )

    deduped = _dedupe_supplies(supplies)
    ordered = sorted(deduped, key=_sort_key)
    return ordered, partial, invalid_count


def _classify_snmp_error(message: str) -> str:
    lower = message.casefold()
    if "timeout" in lower or "timed out" in lower or "no snmp response" in lower:
        return DIAG_SNMP_TIMEOUT
    if "authorization" in lower or "community" in lower or "authentication" in lower:
        return DIAG_SNMP_AUTH
    return message


def _to_int(value: str | int | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _calc_percent(level: int | None, max_value: int | None) -> int | None:
    if level is None or max_value is None:
        return None
    if max_value <= 0 or level < 0:
        return None
    return max(0, min(100, round((level / max_value) * 100)))


def _is_useful_supply(desc: str, class_raw: str | int | None) -> bool:
    normalized = desc.casefold()
    if not normalized:
        return False
    if any(token in normalized for token in ("toner", "ink", "cartridge")):
        return True
    class_value = _to_int(class_raw)
    return class_value in {3, 4}


def _detect_kind(desc: str) -> str:
    lowered = desc.casefold()
    if "ink" in lowered:
        return "ink"
    if "cartridge" in lowered:
        return "cartridge"
    return "toner"


def _detect_color(desc: str) -> str | None:
    lowered = desc.casefold()
    words = re.findall(r"[a-z]+", lowered)
    for word in words:
        if word in COLOR_BY_TOKEN:
            return COLOR_BY_TOKEN[word]
    return None


def _display_name(desc: str, color: str | None, kind: str) -> str:
    if color:
        return color
    cleaned = re.sub(r"\s+", " ", desc).strip()
    if cleaned:
        return cleaned[:24]
    return kind.capitalize()


def _unit_name(raw_unit: str | int | None) -> str | None:
    value = _to_int(raw_unit)
    mapping = {3: "tenThousandthsOfInches", 8: "impressions", 12: "percent"}
    return mapping.get(value)


def _supply_severity(percent: int | None) -> CardSeverity:
    if percent is None:
        return CardSeverity.UNKNOWN
    if percent < 10:
        return CardSeverity.CRITICAL
    if percent < 20:
        return CardSeverity.WARNING
    return CardSeverity.OK


def _dedupe_supplies(supplies: list[SupplyLevel]) -> list[SupplyLevel]:
    by_key: dict[tuple[str, str], SupplyLevel] = {}
    for item in supplies:
        key = (item.kind, item.color or item.name)
        existing = by_key.get(key)
        if not existing:
            by_key[key] = item
            continue
        existing_score = -1 if existing.percent is None else existing.percent
        item_score = -1 if item.percent is None else item.percent
        if item_score >= existing_score:
            by_key[key] = item
    return list(by_key.values())


def _sort_key(item: SupplyLevel) -> tuple[int, int, str]:
    color_order = {"K": 0, "C": 1, "M": 2, "Y": 3}
    is_color = 0 if item.color in color_order else 1
    return is_color, color_order.get(item.color or "", 99), item.name
