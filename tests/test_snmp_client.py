import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from prn_site_ping.snmp_client import normalize_supply_rows


def test_normalize_supply_rows_keeps_supplies_and_handles_invalid_values() -> None:
    rows = {
        "1": {"desc": "Black Toner Cartridge", "max": "100", "level": "72"},
        "2": {"desc": "Cyan Toner Cartridge", "max": "-3", "level": "50"},
        "3": {"desc": "Waste Container", "max": "100", "level": "20"},
    }

    supplies = normalize_supply_rows(rows)

    assert len(supplies) == 2
    assert supplies[0].code == "K"
    assert supplies[0].percent == 72
    assert supplies[1].code == "C"
    assert supplies[1].percent is None
