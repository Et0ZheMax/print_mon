import sys
from pathlib import Path

# Allow running tests without installing the package
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from prn_site_ping.config import read_printers_file  # noqa: E402


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
