"""
Wrapper that brings the legacy `kalshi_edge_finder.py` FRED/NOAA clients
into the pm_bot package namespace. We don't duplicate that code — we
import it so the v1 scanner and v2 strategies stay in sync.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Optional


def _load_legacy_module():
    """Load kalshi_edge_finder.py as a module without treating it as a package."""
    legacy_path = Path(__file__).resolve().parents[2] / "kalshi_edge_finder.py"
    if not legacy_path.exists():
        return None
    spec = importlib.util.spec_from_file_location("kalshi_edge_finder_legacy", legacy_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules["kalshi_edge_finder_legacy"] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    return module


_legacy = _load_legacy_module()


def fred_client() -> Optional[object]:
    """Return an instance of the legacy FREDClient, or None if unavailable."""
    if _legacy is None:
        return None
    try:
        return _legacy.FREDClient()
    except Exception:
        return None


def noaa_client() -> Optional[object]:
    if _legacy is None:
        return None
    try:
        return _legacy.NOAAClient()
    except Exception:
        return None


def run_legacy_scan() -> list:
    """Invoke the v1 edge finder and return its EdgeSignals."""
    if _legacy is None:
        return []
    try:
        finder = _legacy.EdgeFinder()
        return finder.run_full_scan()
    except Exception:
        return []
