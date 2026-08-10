from __future__ import annotations

import logging
import socket
import threading
import time
from datetime import datetime, timezone

from .models import CardSeverity, PrinterStatus, SnmpConfig, SupplyLevel
from .snmp_client import SnmpClient

LOGGER = logging.getLogger(__name__)


class PrinterMonitor:
    def __init__(self, timeout: float, snmp_config: SnmpConfig, dns_cache_ttl: float = 60.0):
        self.timeout = timeout
        self.snmp_client = SnmpClient(snmp_config)
        self.snmp_config = snmp_config
        self.dns_cache_ttl = max(0.0, float(dns_cache_ttl))
        self._dns_cache: dict[str, tuple[float, str | None, str | None]] = {}
        self._web_schemes: dict[str, str] = {}
        self._cache_lock = threading.Lock()

    def _resolve(self, name: str) -> tuple[str | None, str | None]:
        key = name.casefold()
        now = time.monotonic()
        with self._cache_lock:
            cached = self._dns_cache.get(key)
            if cached and cached[0] > now:
                return cached[1], cached[2]

        try:
            ip = socket.gethostbyname(name)
            error = None
            ttl = self.dns_cache_ttl
        except socket.gaierror:
            ip = None
            error = "dns failed"
            ttl = min(self.dns_cache_ttl, 10.0)
        except Exception as exc:
            ip = None
            error = str(exc)
            ttl = min(self.dns_cache_ttl, 10.0)

        with self._cache_lock:
            self._dns_cache[key] = (now + ttl, ip, error)
        return ip, error

    def check_reachability(self, name: str) -> tuple[str | None, bool, str | None]:
        ip, resolve_error = self._resolve(name)
        if not ip:
            LOGGER.info("DNS resolve failed for %s: %s", name, resolve_error)
            return None, False, resolve_error

        last_error: str | None = None
        per_port_timeout = max(0.15, float(self.timeout) / 2)
        for port, scheme in ((80, "http"), (443, "https")):
            try:
                with socket.create_connection((ip, port), timeout=per_port_timeout):
                    with self._cache_lock:
                        self._web_schemes[name.casefold()] = scheme
                    return ip, True, None
            except OSError as exc:
                last_error = str(exc)

        LOGGER.info("Printer %s (%s) has no reachable web port: %s", name, ip, last_error)
        return ip, False, "web unavailable"

    def web_url(self, name: str, resolved_ip: str | None = None) -> str:
        with self._cache_lock:
            scheme = self._web_schemes.get(name.casefold(), "http")
        return f"{scheme}://{resolved_ip or name}"

    def build_status(self, name: str, include_snmp: bool) -> PrinterStatus:
        ip, reachable, reachability_error = self.check_reachability(name)
        supplies: tuple[SupplyLevel, ...] = ()
        snmp_ok = False
        diagnostic = reachability_error

        if include_snmp and ip and self.snmp_config.enabled:
            telemetry = self.snmp_client.fetch_supplies(ip)
            supplies = telemetry.supplies
            snmp_ok = telemetry.ok
            if telemetry.reason:
                diagnostic = telemetry.reason if not diagnostic else f"{diagnostic}; {telemetry.reason}"

        device_reachable = reachable or snmp_ok
        severity = aggregate_severity(
            reachable=device_reachable,
            supplies=list(supplies),
            snmp_ok=snmp_ok,
            warning=self.snmp_config.warning_threshold,
            critical=self.snmp_config.critical_threshold,
        )
        if not self.snmp_config.enabled and device_reachable:
            severity = CardSeverity.OK
        summary = (
            format_supplies_summary(list(supplies), snmp_ok=snmp_ok)
            if self.snmp_config.enabled
            else ""
        )

        return PrinterStatus(
            name=name,
            resolved_ip=ip,
            reachable=device_reachable,
            snmp_ok=snmp_ok,
            supplies=supplies,
            severity=severity,
            summary_text=summary,
            updated_at=datetime.now(timezone.utc),
            last_error=diagnostic,
            diagnostic=diagnostic,
            web_scheme=self._web_schemes.get(name.casefold()),
            snmp_enabled=self.snmp_config.enabled,
        )


def aggregate_severity(reachable: bool, supplies: list[SupplyLevel], snmp_ok: bool, warning: int = 20, critical: int = 10) -> CardSeverity:
    if not reachable:
        return CardSeverity.OFFLINE

    known = [s.percent for s in supplies if s.percent is not None]
    if not known:
        return CardSeverity.UNKNOWN if not snmp_ok else CardSeverity.OK

    worst = min(known)
    if worst <= critical:
        return CardSeverity.CRITICAL
    if worst <= warning:
        return CardSeverity.WARNING
    return CardSeverity.OK


def format_supplies_summary(supplies: list[SupplyLevel], snmp_ok: bool) -> str:
    if not supplies:
        return "SNMP: нет данных" if snmp_ok else "SNMP: недоступен"

    colors = [item for item in supplies if item.color in {"K", "C", "M", "Y"}]
    target = colors if colors else supplies[:4]

    parts: list[str] = []
    for supply in target[:4]:
        label = supply.color or supply.name
        percent_text = "?" if supply.percent is None else str(supply.percent)
        parts.append(f"{label} {percent_text}%")
    return " · ".join(parts)
