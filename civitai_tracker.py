#!/usr/bin/env python3
"""Cross-platform source-mode launcher for the CivitAI Tracker.

Windows users normally start the app with ``launch_tracker.bat`` /
``launch_tracker.pyw``. Those rely on Windows-only paths and console handling,
so this module provides an equivalent entry point that works the same way on
Linux and macOS:

    python civitai_tracker.py                  # start the desktop app
    python civitai_tracker.py --minimized      # start minimized to tray
    python -m civitai_tracker --version

It adds the project directory (and a local ``.venv`` site-packages folder, if
present) to ``sys.path``, then hands off to ``tracker_app.main`` with the
original command-line arguments preserved.
"""

from __future__ import annotations

import os
import site
import sys
from pathlib import Path

MIN_PYTHON = (3, 11)


def _base_dir() -> Path:
    return Path(__file__).resolve().parent


def _add_local_venv_site(base_dir: Path) -> None:
    """Make a project-local ``.venv`` importable without activating it.

    Windows lays the venv out as ``.venv/Lib/site-packages`` and
    ``.venv/Scripts``; POSIX uses ``.venv/lib/pythonX.Y/site-packages`` and
    ``.venv/bin``. Add whichever exists so source-mode works on either.
    """
    venv = base_dir / ".venv"
    if not venv.exists():
        return

    candidates = [venv / "Lib" / "site-packages"]  # Windows
    candidates.extend(sorted(venv.glob("lib/python*/site-packages")))  # POSIX
    for site_packages in candidates:
        if site_packages.exists():
            site.addsitedir(str(site_packages))

    for bin_dir in (venv / "Scripts", venv / "bin"):
        if bin_dir.exists():
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


def main() -> int:
    if sys.version_info < MIN_PYTHON:
        have = ".".join(map(str, sys.version_info[:3]))
        need = ".".join(map(str, MIN_PYTHON))
        sys.stderr.write(
            f"CivitAI Tracker needs Python {need}+ (found {have}).\n"
        )
        return 1

    base_dir = _base_dir()
    os.chdir(base_dir)

    tracker_script = base_dir / "tracker_app.py"
    if not tracker_script.exists():
        sys.stderr.write(f"tracker_app.py was not found at {tracker_script}\n")
        return 1

    _add_local_venv_site(base_dir)
    if str(base_dir) not in sys.path:
        sys.path.insert(0, str(base_dir))

    try:
        import tracker_app
    except ImportError as exc:
        sys.stderr.write(
            f"Could not import the tracker ({exc}).\n"
            "Install dependencies with: python -m pip install -r requirements.txt\n"
        )
        return 1

    return tracker_app.main() or 0


if __name__ == "__main__":
    raise SystemExit(main())
