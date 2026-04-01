from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class CardSeverity(str, Enum):
    OFFLINE = "offline"
    OK = "ok"
    WARNING = "warning"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SnmpConfig:
    enabled: bool = True
    community: str = "public"
    port: int = 161
    timeout: float = 1.2
    retries: int = 1
    refresh_interval: int = 300
    warning_threshold: int = 20
    critical_threshold: int = 10


@dataclass(frozen=True)
class SupplyLevel:
    code: str
    label: str
    percent: int | None
    raw_level: int | None = None
    raw_max: int | None = None


@dataclass(frozen=True)
class PrinterStatus:
    name: str
    resolved_ip: str | None
    reachable: bool
    snmp_ok: bool
    supplies: tuple[SupplyLevel, ...] = ()
    severity: CardSeverity = CardSeverity.UNKNOWN
    summary_text: str = ""
    updated_at: datetime = field(default_factory=datetime.utcnow)
    last_error: str | None = None
