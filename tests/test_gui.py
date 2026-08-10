import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import pytest

from prn_site_ping.gui import (  # noqa: E402
    PrinterCard,
    PrinterDashboard,
    _compose_card_summary,
    _responsive_columns,
    _validated_snmp_config,
)


def test_sort_printers_orders_names_case_insensitively() -> None:
    printers = ["zeta-2", "Alpha-1", "beta-3", "alpha-2"]

    assert PrinterDashboard._sort_printers(printers) == [
        "Alpha-1",
        "alpha-2",
        "beta-3",
        "zeta-2",
    ]


def test_compose_card_summary_appends_diagnostic_line() -> None:
    assert _compose_card_summary("SNMP: нет данных", "SNMP timeout") == "SNMP: нет данных\ndiag: SNMP timeout"


def test_compose_card_summary_without_diagnostic_keeps_summary() -> None:
    assert _compose_card_summary("K 50%", None) == "K 50%"


def test_responsive_columns_respects_space_and_configured_maximum() -> None:
    assert _responsive_columns(500, configured_max=4) == 1
    assert _responsive_columns(960, configured_max=4) == 3
    assert _responsive_columns(2400, configured_max=4) == 4


def test_snmp_settings_validate_threshold_order() -> None:
    with pytest.raises(ValueError, match="критический"):
        _validated_snmp_config(
            enabled=True,
            community="public",
            port="161",
            timeout="1,2",
            retries="1",
            refresh_interval="300",
            warning_threshold="10",
            critical_threshold="20",
        )


def test_snmp_settings_accept_decimal_comma() -> None:
    config = _validated_snmp_config(
        enabled=True,
        community="public",
        port="161",
        timeout="1,5",
        retries="1",
        refresh_interval="300",
        warning_threshold="20",
        critical_threshold="10",
    )
    assert config.timeout == 1.5


def test_card_hover_does_not_change_border_thickness() -> None:
    source = Path(PrinterCard._on_enter.__code__.co_filename).read_text(encoding="utf-8")
    hover_body = source.split("def _on_enter", 1)[1].split("def _on_leave", 1)[0]
    assert "highlightthickness" not in hover_body
