from __future__ import annotations

import logging
import socket
from datetime import datetime

from .models import CardSeverity, PrinterStatus, SnmpConfig, SupplyLevel
from .snmp_client import SnmpClient

LOGGER = logging.getLogger(__name__)


class PrinterMonitor:
    def __init__(self, timeout: float, snmp_config: SnmpConfig):
        self.timeout = timeout
        self.snmp_client = SnmpClient(snmp_config)
        self.snmp_config = snmp_config

    def check_reachability(self, name: str) -> tuple[str | None, bool, str | None]:
        try:
            ip = socket.gethostbyname(name)
        except socket.gaierror as exc:
            LOGGER.error("DNS resolve failed for %s: %s", name, exc)
            return None, False, "dns failed"
        except Exception as exc:
            LOGGER.error("Resolve error for %s: %s", name, exc)
            return None, False, str(exc)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(float(self.timeout))
        try:
            result = sock.connect_ex((ip, 80))
        except Exception as exc:
            LOGGER.error("TCP check failed for %s (%s): %s", name, ip, exc)
            return ip, False, str(exc)
        finally:
            sock.close()

        if result != 0:
            LOGGER.info("Printer %s (%s) not reachable on tcp/80; code=%s", name, ip, result)
        return ip, result == 0, None

    def build_status(self, name: str, include_snmp: bool) -> PrinterStatus:
        ip, reachable, reachability_error = self.check_reachability(name)
        supplies: list[SupplyLevel] = []
        snmp_ok = False
        error_text = reachability_error

        if include_snmp and ip:
            supplies, snmp_ok, snmp_error = self.snmp_client.fetch_supplies(ip)
            if snmp_error:
                LOGGER.info("SNMP failed for %s (%s): %s", name, ip, snmp_error)
                error_text = snmp_error if not error_text else f"{error_text}; {snmp_error}"

        severity = aggregate_severity(
            reachable=reachable,
            supplies=supplies,
            snmp_ok=snmp_ok,
            warning=self.snmp_config.warning_threshold,
            critical=self.snmp_config.critical_threshold,
        )
        summary = format_supplies_summary(supplies, snmp_ok=snmp_ok)

        return PrinterStatus(
            name=name,
            resolved_ip=ip,
            reachable=reachable,
            snmp_ok=snmp_ok,
            supplies=tuple(supplies),
            severity=severity,
            summary_text=summary,
            updated_at=datetime.utcnow(),
            last_error=error_text,
        )


def aggregate_severity(reachable: bool, supplies: list[SupplyLevel], snmp_ok: bool, warning: int = 20, critical: int = 10) -> CardSeverity:
    if not reachable:
        return CardSeverity.OFFLINE

    known = [s.percent for s in supplies if s.percent is not None]
    if not known:
        return CardSeverity.UNKNOWN if not snmp_ok else CardSeverity.OK

    worst = min(known)
    if worst < critical:
        return CardSeverity.CRITICAL
    if worst < warning:
        return CardSeverity.WARNING
    return CardSeverity.OK


def format_supplies_summary(supplies: list[SupplyLevel], snmp_ok: bool) -> str:
    if not supplies:
        return "SNMP: нет данных" if not snmp_ok else "Расходники: нет данных"

    parts: list[str] = []
    for supply in supplies[:4]:
        percent_text = "?" if supply.percent is None else str(supply.percent)
        parts.append(f"{supply.code} {percent_text}%")
    return " · ".join(parts)
