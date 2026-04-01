import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from prn_site_ping.models import SnmpConfig, SnmpTelemetryResult, SupplyLevel
from prn_site_ping.monitoring import format_supplies_summary
from prn_site_ping.snmp_adapters import AdapterRegistry, KyoceraEcosysAdapter
from prn_site_ping.snmp_client import (
    DIAG_ADAPTER_NOT_MATCHED,
    DIAG_IDENTITY_NOT_DETECTED,
    SnmpClient,
)
from prn_site_ping.snmp_identity import (
    SYS_DESCR_OID,
    SYS_OBJECT_ID_OID,
    detect_printer_identity,
)


def test_identity_discovery_detects_kyocera_ecosys_model() -> None:
    identity = detect_printer_identity(
        {
            SYS_DESCR_OID: "KYOCERA Document Solutions Inc. ECOSYS M5526cdn",
            SYS_OBJECT_ID_OID: "1.3.6.1.4.1.1347.43.5.1",
        }
    )

    assert identity.vendor == "kyocera"
    assert identity.family == "ecosys"
    assert identity.model == "M5526CDN"


def test_adapter_registry_matches_vendor_family() -> None:
    identity = detect_printer_identity(
        {
            SYS_DESCR_OID: "KYOCERA ECOSYS M8130cidn",
            SYS_OBJECT_ID_OID: "1.3.6.1.4.1.1347.43.5.1",
        }
    )
    adapter = AdapterRegistry().match(identity)
    assert adapter is not None
    assert adapter.name == "kyocera_adapter"


def test_kyocera_adapter_parses_percent_values() -> None:
    adapter = KyoceraEcosysAdapter()
    identity = detect_printer_identity({SYS_DESCR_OID: "ECOSYS M5526cdn"})

    class Ops:
        def walk(self, base_oid: str):
            if base_oid.endswith("43.5.4.1"):
                return {"1.3.6.1.4.1.1347.43.5.4.1.1": "Black Toner 72%", "1.3.6.1.4.1.1347.43.5.4.1.2": "Cyan Toner 61%"}
            return {}

    result = adapter.fetch_supplies(Ops(), identity)

    assert len(result.supplies) == 2
    assert result.supplies[0].source == "kyocera_adapter"


def test_fallback_flow_standard_empty_adapter_returns_supplies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("prn_site_ping.snmp_client._import_pysnmp", lambda: ({}, None))
    client = SnmpClient(SnmpConfig())

    monkeypatch.setattr(client, "_fetch_standard_supplies", lambda host, api: SnmpTelemetryResult(ok=True, supplies=(), reason="standard printer mib supplies not available"))
    monkeypatch.setattr(
        client,
        "_get_oids",
        lambda host, api, oids: {
            SYS_DESCR_OID: "KYOCERA ECOSYS M5526cdn",
            SYS_OBJECT_ID_OID: "1.3.6.1.4.1.1347.43.5.1",
        },
    )

    class FakeAdapter:
        name = "kyocera_adapter"

        def fetch_supplies(self, ops, identity):
            return type("R", (), {"supplies": (SupplyLevel(name="K", kind="toner", color="K", percent=72, source="kyocera_adapter"),), "reason": None, "partial": False})()

    monkeypatch.setattr(client.adapter_registry, "match", lambda identity: FakeAdapter())

    result = client.fetch_supplies("10.0.0.10")

    assert result.ok is True
    assert result.source == "kyocera_adapter"
    assert format_supplies_summary(list(result.supplies), snmp_ok=True) == "K 72%"


def test_fallback_flow_standard_empty_no_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("prn_site_ping.snmp_client._import_pysnmp", lambda: ({}, None))
    client = SnmpClient(SnmpConfig())
    monkeypatch.setattr(client, "_fetch_standard_supplies", lambda host, api: SnmpTelemetryResult(ok=True, supplies=(), reason="standard printer mib supplies not available"))
    monkeypatch.setattr(client, "_get_oids", lambda host, api, oids: {SYS_DESCR_OID: "Unknown Device"})
    monkeypatch.setattr(client.adapter_registry, "match", lambda identity: None)

    result = client.fetch_supplies("10.0.0.11")

    assert result.reason == DIAG_ADAPTER_NOT_MATCHED


def test_fallback_flow_identity_not_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("prn_site_ping.snmp_client._import_pysnmp", lambda: ({}, None))
    client = SnmpClient(SnmpConfig())
    monkeypatch.setattr(client, "_fetch_standard_supplies", lambda host, api: SnmpTelemetryResult(ok=True, supplies=(), reason="standard printer mib supplies not available"))
    monkeypatch.setattr(client, "_get_oids", lambda host, api, oids: {})

    result = client.fetch_supplies("10.0.0.12")

    assert result.reason == DIAG_IDENTITY_NOT_DETECTED


def test_no_manual_oid_configuration_required() -> None:
    cfg = SnmpConfig()
    assert not hasattr(cfg, "oid")
    assert "oid" not in cfg.__dataclass_fields__


def test_standard_path_not_broken_when_supplies_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("prn_site_ping.snmp_client._import_pysnmp", lambda: ({}, None))
    client = SnmpClient(SnmpConfig())
    std = SnmpTelemetryResult(ok=True, supplies=(SupplyLevel(name="K", kind="toner", color="K", percent=80),), source="standard_printer_mib")
    monkeypatch.setattr(client, "_fetch_standard_supplies", lambda host, api: std)

    def _should_not_call(*_args, **_kwargs):
        raise AssertionError("identity discovery should not run")

    monkeypatch.setattr(client, "_get_oids", _should_not_call)
    result = client.fetch_supplies("10.0.0.13")
    assert result.supplies == std.supplies
    assert result.source == "standard_printer_mib"
