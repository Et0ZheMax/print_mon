from __future__ import annotations

import platform
import subprocess


def fetch_printers_from_server(server: str, timeout: float = 8.0) -> list[str]:
    """Fetch printer queue names from a Windows print server.

    Uses PowerShell `Get-Printer -ComputerName <server>` and returns queue names.
    Returns an empty list on unsupported platforms.
    """
    if platform.system().lower() != "windows":
        return []

    cmd = [
        "powershell",
        "-NoProfile",
        "-Command",
        (
            "Get-Printer -ComputerName '"
            + server
            + "' | Select-Object -ExpandProperty Name"
        ),
    ]

    proc = subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
        timeout=max(1.0, float(timeout)),
    )
    return _parse_printer_names(proc.stdout)


def _parse_printer_names(raw: str) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()

    for line in raw.splitlines():
        name = line.strip()
        if not name:
            continue
        if name in seen:
            continue
        seen.add(name)
        names.append(name)

    return names

