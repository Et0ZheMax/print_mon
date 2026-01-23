from __future__ import annotations

import os
from pathlib import Path
from importlib import resources


ENV_CONFIG_PATH = "PRN_SITE_PING_CONFIG"


def load_printers(config_path: str | None = None) -> list[str]:
    """Load printer names from a text file.

    Rules:
      - one printer per line
      - blank lines ignored
      - lines starting with # ignored

    Resolution order:
      1) explicit config_path
      2) env PRN_SITE_PING_CONFIG
      3) ./config/printers.local.txt
      4) ./config/printers.txt
      5) packaged default (prn_site_ping/data/default_printers.txt)

    Raises:
      FileNotFoundError if nothing found.
    """

    candidates: list[Path] = []

    if config_path:
        candidates.append(Path(config_path))

    env = os.environ.get(ENV_CONFIG_PATH)
    if env:
        candidates.append(Path(env))

    cwd = Path.cwd()
    candidates.append(cwd / "config" / "printers.local.txt")
    candidates.append(cwd / "config" / "printers.txt")

    for path in candidates:
        if path.is_file():
            return _parse_printer_file(path)

    # packaged default
    try:
        with resources.files("prn_site_ping").joinpath("data/default_printers.txt").open(
            "r", encoding="utf-8"
        ) as f:
            return _parse_lines(f.read().splitlines())
    except Exception as e:
        raise FileNotFoundError(
            "Не найден файл со списком принтеров. "
            "Укажи --config ./config/printers.txt или создай его."
        ) from e


def resolve_printers_path(config_path: str | None = None) -> Path:
    """Resolve a writable path for the printers list."""
    if config_path:
        return Path(config_path)

    env = os.environ.get(ENV_CONFIG_PATH)
    if env:
        return Path(env)

    return Path.cwd() / "config" / "printers.local.txt"


def write_printers_file(path: Path | str, printers: list[str]) -> None:
    """Write printer names to a file, one per line."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(printers) + ("\n" if printers else "")
    p.write_text(content, encoding="utf-8")


def read_printers_file(path: Path | str) -> list[str]:
    """Parse a printers file (public helper, handy for tests).

    See `load_printers` for format rules.
    """
    p = Path(path)
    return _parse_printer_file(p)


def _parse_printer_file(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return _parse_lines(lines)


def _parse_lines(lines: list[str]) -> list[str]:
    printers: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        printers.append(line)

    # unique + stable
    seen: set[str] = set()
    out: list[str] = []
    for p in printers:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out
