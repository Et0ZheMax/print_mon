import sys
from contextlib import nullcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from prn_site_ping.models import CardSeverity, SnmpConfig, SupplyLevel
from prn_site_ping.monitoring import PrinterMonitor, aggregate_severity, format_supplies_summary


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


def test_aggregate_severity_includes_threshold_boundary() -> None:
    warning = [SupplyLevel(name="K", kind="toner", color="K", percent=20)]
    critical = [SupplyLevel(name="K", kind="toner", color="K", percent=10)]
    assert aggregate_severity(True, warning, True, warning=20, critical=10) == CardSeverity.WARNING
    assert aggregate_severity(True, critical, True, warning=20, critical=10) == CardSeverity.CRITICAL


def test_reachability_reuses_dns_result(monkeypatch) -> None:
    lookups: list[str] = []

    def resolve(name: str) -> str:
        lookups.append(name)
        return "192.0.2.10"

    monkeypatch.setattr("prn_site_ping.monitoring.socket.gethostbyname", resolve)
    monkeypatch.setattr(
        "prn_site_ping.monitoring.socket.create_connection",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monitor = PrinterMonitor(1.0, SnmpConfig(enabled=False), dns_cache_ttl=60)

    assert monitor.check_reachability("PRN-1")[1] is True
    assert monitor.check_reachability("PRN-1")[1] is True
    assert lookups == ["PRN-1"]


def test_reachability_falls_back_to_https(monkeypatch) -> None:
    monkeypatch.setattr("prn_site_ping.monitoring.socket.gethostbyname", lambda _name: "192.0.2.10")

    def connect(address, **_kwargs):
        if address[1] == 80:
            raise OSError("closed")
        return nullcontext()

    monkeypatch.setattr("prn_site_ping.monitoring.socket.create_connection", connect)
    monitor = PrinterMonitor(1.0, SnmpConfig(enabled=False))

    _, reachable, _ = monitor.check_reachability("PRN-HTTPS")
    assert reachable is True
    assert monitor.web_url("PRN-HTTPS") == "https://PRN-HTTPS"


def test_disabled_snmp_does_not_report_missing_details(monkeypatch) -> None:
    monitor = PrinterMonitor(1.0, SnmpConfig(enabled=False))
    monkeypatch.setattr(
        monitor,
        "check_reachability",
        lambda _name: ("192.0.2.10", True, None),
    )

    status = monitor.build_status("PRN-1", include_snmp=True)

    assert status.snmp_enabled is False
    assert status.severity == CardSeverity.OK
    assert status.summary_text == ""
    assert status.diagnostic is None
