import sys
from pathlib import Path

# Allow running tests without installing the package
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from prn_site_ping.config import read_printers_file, write_printers_file  # noqa: E402


def test_read_printers_file_ignores_comments_and_blanks(tmp_path: Path) -> None:
    p = tmp_path / "printers.txt"
    p.write_text(
        """
# comment
PRN-1

PRN-2
PRN-1
""".strip(),
        encoding="utf-8",
    )

    assert read_printers_file(p) == ["PRN-1", "PRN-2"]


def test_printer_names_are_deduplicated_case_insensitively(tmp_path: Path) -> None:
    p = tmp_path / "printers.txt"
    p.write_text("PRN-1\nprn-1\nPRN-2\n", encoding="utf-8")
    assert read_printers_file(p) == ["PRN-1", "PRN-2"]


def test_write_printers_file_replaces_content(tmp_path: Path) -> None:
    p = tmp_path / "nested" / "printers.txt"
    write_printers_file(p, ["PRN-1", "PRN-2"])
    assert p.read_text(encoding="utf-8") == "PRN-1\nPRN-2\n"
