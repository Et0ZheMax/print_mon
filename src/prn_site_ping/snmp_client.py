from __future__ import annotations

import logging
import re
from collections import defaultdict

from .models import SnmpConfig, SupplyLevel

LOGGER = logging.getLogger(__name__)

SUPPLIES_DESC_BASE = "1.3.6.1.2.1.43.11.1.1.6.1"
SUPPLIES_MAX_BASE = "1.3.6.1.2.1.43.11.1.1.8.1"
SUPPLIES_LEVEL_BASE = "1.3.6.1.2.1.43.11.1.1.9.1"

CODE_ALIASES: dict[str, tuple[str, ...]] = {
    "K": ("black", "bk", "k"),
    "C": ("cyan", "c"),
    "M": ("magenta", "m"),
    "Y": ("yellow", "y"),
}


class SnmpClient:
    def __init__(self, cfg: SnmpConfig):
        self.cfg = cfg

    def fetch_supplies(self, host: str) -> tuple[list[SupplyLevel], bool, str | None]:
        if not self.cfg.enabled:
            return [], False, "snmp disabled"

        try:
            from pysnmp.hlapi import (
                CommunityData,
                ContextData,
                ObjectIdentity,
                ObjectType,
                SnmpEngine,
                UdpTransportTarget,
                nextCmd,
            )
        except Exception:
            LOGGER.warning("SNMP library unavailable: pysnmp not installed")
            return [], False, "snmp library unavailable"

        rows: dict[str, dict[str, str | int]] = defaultdict(dict)
        bases = {
            SUPPLIES_DESC_BASE: "desc",
            SUPPLIES_MAX_BASE: "max",
            SUPPLIES_LEVEL_BASE: "level",
        }

        try:
            iterator = nextCmd(
                SnmpEngine(),
                CommunityData(self.cfg.community, mpModel=1),
                UdpTransportTarget((host, int(self.cfg.port)), timeout=float(self.cfg.timeout), retries=int(self.cfg.retries)),
                ContextData(),
                ObjectType(ObjectIdentity("1.3.6.1.2.1.43.11.1.1")),
                lexicographicMode=False,
            )

            for error_indication, error_status, error_index, var_binds in iterator:
                if error_indication:
                    return [], False, str(error_indication)
                if error_status:
                    return [], False, f"{error_status.prettyPrint()} at {error_index}"

                for oid_obj, value_obj in var_binds:
                    oid = oid_obj.prettyPrint()
                    value = value_obj.prettyPrint()
                    for base, key in bases.items():
                        if oid.startswith(base + "."):
                            idx = oid[len(base) + 1 :]
                            rows[idx][key] = value
                            break

        except Exception as exc:
            LOGGER.error("SNMP query failed for %s: %s", host, exc)
            return [], False, str(exc)

        supplies = normalize_supply_rows(rows)
        return supplies, True, None


def normalize_supply_rows(rows: dict[str, dict[str, str | int]]) -> list[SupplyLevel]:
    supplies: list[SupplyLevel] = []
    for idx, raw in rows.items():
        desc = str(raw.get("desc", "")).strip()
        if not _is_supply_desc(desc):
            if desc:
                LOGGER.debug("Skipping non-supply SNMP row %s: %s", idx, desc)
            continue

        code, label = _to_code_and_label(desc)
        max_value = _to_int(raw.get("max"))
        level_value = _to_int(raw.get("level"))
        percent = _calc_percent(level_value, max_value)
        if percent is None and (max_value is not None or level_value is not None):
            LOGGER.info("Invalid supply row %s (%s): max=%s level=%s", idx, desc, max_value, level_value)

        supplies.append(
            SupplyLevel(code=code, label=label, percent=percent, raw_level=level_value, raw_max=max_value)
        )

    order = {"K": 0, "C": 1, "M": 2, "Y": 3}
    return sorted(supplies, key=lambda item: (0 if item.code in order else 1, order.get(item.code, 99), item.code))


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


def _is_supply_desc(desc: str) -> bool:
    normalized = desc.casefold()
    return any(token in normalized for token in ("toner", "ink", "cartridge", "drum", "developer"))


def _to_code_and_label(desc: str) -> tuple[str, str]:
    lowered = desc.casefold()
    compact = re.sub(r"[^a-z]", "", lowered)
    for code, aliases in CODE_ALIASES.items():
        if compact == code.casefold() or any(alias in lowered.split() for alias in aliases) or any(alias in compact for alias in aliases):
            return code, code
    cleaned = re.sub(r"\s+", " ", desc).strip()
    return cleaned[:10], cleaned
