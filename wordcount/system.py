"""Platform-specific paths of external programs, from the [system] config section."""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .common import Config

# Tried in order; the first that exists wins
DEFAULTS = {
    "linux": {
        "soffice": ["soffice", "libreoffice"],
        "lo_python": ["/usr/bin/python3"],
    },
    "windows": {
        "soffice": [r"C:\Program Files\LibreOffice\program\soffice.exe",
                    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe", "soffice"],
        "lo_python": [r"C:\Program Files\LibreOffice\program\python.exe",
                      r"C:\Program Files (x86)\LibreOffice\program\python.exe"],
    },
    "macos": {
        "soffice": ["/Applications/LibreOffice.app/Contents/MacOS/soffice", "soffice"],
        "lo_python": ["/Applications/LibreOffice.app/Contents/Resources/python"],
    },
}


@dataclass
class System:
    platform: str
    pandoc: str | None
    soffice: str | None
    lo_python: str | None


def detect_platform() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def find(candidates: list[str]) -> str | None:
    """First candidate that is an existing file or a command on PATH."""
    for c in candidates:
        if Path(c).is_file():
            return c
        if found := shutil.which(c):
            return found
    return None


def resolve(cfg: Config) -> System:
    s = cfg.get("system", {})
    platform = s.get("platform", "auto")
    if platform == "auto":
        platform = detect_platform()
    if platform not in DEFAULTS:
        raise ValueError(f"[system] platform must be auto, {', '.join(DEFAULTS)}; got {platform!r}")
    defaults = DEFAULTS[platform]

    def pick(key: str, default: list[str]) -> str | None:
        value = s.get(key, "")
        return find([value]) if value else find(default)

    return System(platform,
                  pandoc=pick("pandoc", ["pandoc"]),
                  soffice=pick("soffice", defaults["soffice"]),
                  lo_python=pick("lo_python", defaults["lo_python"]))


def utf8_env() -> dict:
    """Environment for child Pythons, so they read and write UTF-8 on every OS."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env
