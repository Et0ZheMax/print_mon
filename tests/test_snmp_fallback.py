import builtins
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from prn_site_ping.models import SnmpConfig
from prn_site_ping.snmp_client import DIAG_SNMP_LIB_MISSING, SnmpClient


def test_snmp_client_graceful_when_library_missing(monkeypatch) -> None:
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("pysnmp"):
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    client = SnmpClient(SnmpConfig())
    result = client.fetch_supplies("127.0.0.1")

    assert result.supplies == ()
    assert result.ok is False
    assert result.reason == DIAG_SNMP_LIB_MISSING
