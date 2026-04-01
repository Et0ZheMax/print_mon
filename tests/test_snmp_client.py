import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from prn_site_ping import snmp_client
from prn_site_ping.snmp_client import (
    DIAG_SNMP_LIB_MISSING,
    SnmpClient,
    _parse_supplies_table_oid,
    normalize_supply_rows,
)
from prn_site_ping.models import SnmpConfig


def test_normalize_supply_rows_parses_standard_rows_and_marks_partial() -> None:
    rows = {
        "1": {"desc": "Black Toner Cartridge", "max": "100", "level": "72", "class": "3", "unit": "12"},
        "2": {"desc": "Cyan Toner Cartridge", "max": "-3", "level": "50", "class": "3", "unit": "12"},
        "3": {"desc": "Waste Container", "max": "100", "level": "20", "class": "5", "unit": "12"},
    }

    supplies, partial, invalid_count = normalize_supply_rows(rows)

    assert len(supplies) == 2
    assert supplies[0].name == "K"
    assert supplies[0].percent == 72
    assert supplies[1].name == "C"
    assert supplies[1].percent is None
    assert partial is True
    assert invalid_count == 1


def test_normalize_supply_rows_handles_invalid_and_empty_values() -> None:
    rows = {"1": {"desc": "", "max": "0", "level": "-1", "class": "3"}}
    supplies, partial, invalid_count = normalize_supply_rows(rows)
    assert supplies == []
    assert partial is False
    assert invalid_count == 0


def test_snmp_library_missing_degrades_without_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(snmp_client, "_import_pysnmp", lambda: (None, DIAG_SNMP_LIB_MISSING))
    cfg = SnmpConfig(enabled=True)
    client = SnmpClient(cfg)
    result = client.fetch_supplies("127.0.0.1")
    assert result.ok is False
    assert result.reason == DIAG_SNMP_LIB_MISSING


def test_parse_supplies_table_oid_keeps_full_row_index() -> None:
    oid = "1.3.6.1.2.1.43.11.1.1.9.7.42"
    parsed = _parse_supplies_table_oid(oid)
    assert parsed == ("9", "7.42")


def test_parse_supplies_table_oid_accepts_standard_entry_shape() -> None:
    oid = "1.3.6.1.2.1.43.11.1.1.6.1.3"
    parsed = _parse_supplies_table_oid(oid)
    assert parsed == ("6", "1.3")


def test_parse_supplies_table_oid_rejects_short_or_outside_tree() -> None:
    assert _parse_supplies_table_oid("1.3.6.1.2.1.43.11.1.1.6") is None
    assert _parse_supplies_table_oid("1.3.6.1.2.1.43.10.1.1.6.1.1") is None
