from __future__ import annotations

import platform
import re
import subprocess


_SERVER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,252}[A-Za-z0-9])?$")


def normalize_server_name(server: str) -> str:
    normalized = server.strip().removeprefix("\\\\")
    if not normalized or not _SERVER_RE.fullmatch(normalized):
        raise ValueError("Некорректное имя print-сервера")
    return normalized


def fetch_printers_from_server(server: str, timeout: float = 8.0) -> list[str]:
    """Fetch printer queue names from a Windows print server.

    Uses PowerShell `Get-Printer -ComputerName <server>` and returns queue names.
    Returns an empty list on unsupported platforms.
    """
    if platform.system().lower() != "windows":
        return []

    server = normalize_server_name(server)

    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
        f"Get-Printer -ComputerName '{server}' | Select-Object -ExpandProperty Name",
    ]

    proc = subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=max(1.0, float(timeout)),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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

