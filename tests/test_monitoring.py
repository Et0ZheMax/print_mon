import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from prn_site_ping.models import CardSeverity, SupplyLevel
from prn_site_ping.monitoring import aggregate_severity, format_supplies_summary


def test_aggregate_severity_uses_worst_supply() -> None:
    supplies = [
        SupplyLevel(name="K", kind="toner", color="K", percent=55),
        SupplyLevel(name="C", kind="toner", color="C", percent=9),
    ]

    severity = aggregate_severity(reachable=True, supplies=supplies, snmp_ok=True, warning=20, critical=10)
    assert severity == CardSeverity.CRITICAL


def test_format_supplies_summary_with_partial_color_data() -> None:
    supplies = [
        SupplyLevel(name="K", kind="toner", color="K", percent=72),
        SupplyLevel(name="C", kind="toner", color="C", percent=None, is_unknown=True),
        SupplyLevel(name="M", kind="toner", color="M", percent=58),
    ]
    assert format_supplies_summary(supplies, snmp_ok=True) == "K 72% · C ?% · M 58%"


def test_format_supplies_summary_fallback_without_snmp_data() -> None:
    assert format_supplies_summary([], snmp_ok=False) == "SNMP: недоступен"


def test_aggregate_severity_without_snmp_is_unknown() -> None:
    severity = aggregate_severity(reachable=True, supplies=[], snmp_ok=False)
    assert severity == CardSeverity.UNKNOWN
