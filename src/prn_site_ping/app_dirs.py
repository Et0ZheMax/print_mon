from __future__ import annotations

import os
import sys
from pathlib import Path


def get_app_data_dir(app_name: str = "prn-site-ping") -> Path:
    """Return an OS-appropriate directory for app state/logs.

    Tries:
      - Windows: %APPDATA%\app_name
      - macOS: ~/Library/Application Support/app_name
      - Linux/Unix: ~/.config/app_name
    Falls back to current working directory if something goes wrong.
    """

    try:
        home = Path.home()

        if sys.platform.startswith("win"):
            base = os.environ.get("APPDATA")
            if base:
                return (Path(base) / app_name)
            return (home / "AppData" / "Roaming" / app_name)

        if sys.platform == "darwin":
            return (home / "Library" / "Application Support" / app_name)

        # linux + other unix
        xdg = os.environ.get("XDG_CONFIG_HOME")
        if xdg:
            return (Path(xdg) / app_name)
        return (home / ".config" / app_name)

    except Exception:
        return Path.cwd()
