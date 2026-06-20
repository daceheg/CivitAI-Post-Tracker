"""Buzz balance snapshot ingest (PR-5).

Records a daily snapshot of the account's buzz balances so the dashboard can
chart them over time. CivitAI exposes all three balances in a single authed
call:

    buzz.getBuzzAccount?input={"json":{"authed":true}}  ->  {blue, green, yellow}

(blue = earned, yellow = purchased, green = generation/free buzz.)

Best-effort and self-contained, mirroring ``follower_ingest``: any failure
returns a summary dict with ``ok=False`` and never raises into the tracker run.
Reuses the host/key/HTTP helpers already in ``buzz_ingest`` to stay on the same
request path that works in production.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from app_info import APP_VERSION
from buzz_ingest import (
    extract_response_root,
    iso_z,
    make_trpc_url,
    utc_now,
)

TABLE = "buzz_balance_snapshots"
GET_BUZZ_ACCOUNT_PROC = "buzz.getBuzzAccount"
BUZZ_TYPES = ("blue", "green", "yellow")


def _headers(api_key: str) -> Dict[str, str]:
    return {
        "User-Agent": f"civitai-post-tracker-v{APP_VERSION}-core/1.0",
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
    }


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


# ------------------------------
# Schema
# ------------------------------

def ensure_buzz_balance_schema(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at TEXT NOT NULL,
                snapshot_date TEXT NOT NULL UNIQUE,
                blue_balance INTEGER,
                green_balance INTEGER,
                yellow_balance INTEGER
            )
            """
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_captured_at ON {TABLE}(captured_at)"
        )
        conn.commit()
    finally:
        conn.close()


# ------------------------------
# Parsing (shape confirmed live; tolerant of missing fields)
# ------------------------------

def parse_buzz_balances(account_node: Dict[str, Any]) -> Dict[str, Optional[int]]:
    node = account_node if isinstance(account_node, dict) else {}
    return {
        "blue_balance": _safe_int(node.get("blue")),
        "green_balance": _safe_int(node.get("green")),
        "yellow_balance": _safe_int(node.get("yellow")),
    }


# ------------------------------
# Fetch
# ------------------------------

def fetch_buzz_balances(
    session: requests.Session,
    host: str,
    timeout: int = 60,
) -> Dict[str, Optional[int]]:
    """Fetch the current buzz balances (no DB write). Self-scoped to the key."""
    url = make_trpc_url(host, GET_BUZZ_ACCOUNT_PROC, {"json": {"authed": True}})
    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()
    # buzz.getBuzzAccount returns the {result:{data:{json:...}}} envelope;
    # extract_response_root unwraps the inner `json` payload.
    node = extract_response_root(resp.json())
    return parse_buzz_balances(node if isinstance(node, dict) else {})


def write_snapshot(db_path: str, fields: Dict[str, Optional[int]], captured_at: Optional[str] = None) -> bool:
    """Insert one snapshot, deduped to one row per UTC day. Returns True if written."""
    ensure_buzz_balance_schema(db_path)
    captured = captured_at or iso_z(utc_now())
    snapshot_date = captured[:10]  # YYYY-MM-DD (UTC)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            f"""
            INSERT OR IGNORE INTO {TABLE} (
                captured_at, snapshot_date, blue_balance, green_balance, yellow_balance
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                captured,
                snapshot_date,
                fields.get("blue_balance"),
                fields.get("green_balance"),
                fields.get("yellow_balance"),
            ),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ------------------------------
# Orchestration (called from run_collection_once)
# ------------------------------

def run_buzz_balance_ingest(runtime: Dict[str, Any], db_path: str) -> Dict[str, Any]:
    """Fetch + store one buzz balance snapshot. Never raises."""
    summary: Dict[str, Any] = {"ok": False, "written": False}
    api_key = runtime.get("api_key")
    if not api_key:
        summary["reason"] = "API key required"
        return summary

    host = str(runtime.get("view_host") or "https://civitai.red").rstrip("/")
    timeout = int(runtime.get("timeout") or 60)
    try:
        session = requests.Session()
        session.headers.update(_headers(api_key))
        fields = fetch_buzz_balances(session, host, timeout)
        written = write_snapshot(db_path, fields)
        summary.update(
            ok=True,
            written=written,
            blue_balance=fields.get("blue_balance"),
            green_balance=fields.get("green_balance"),
            yellow_balance=fields.get("yellow_balance"),
        )
    except Exception as exc:  # noqa: BLE001
        summary["error"] = str(exc)
    return summary
