"""Follower / account-standing snapshot ingest (PR-4).

Captures a daily snapshot of the configured account's follower count, creator
rank, and moderation "Account Standing" so the dashboard can chart follower
growth against post-publish events.

Data sources (authed tRPC on the app's host, using the existing API key — no
browser cookies needed; see the endpoint-discovery notes):
  - ``user.getCreator`` -> ``.stats.followerCountAllTime`` (+ reaction/upload/
    generation counts) and ``.rank.leaderboardRank``.
  - ``strike.getMyStrikeSummary`` -> ``{activeStrikes, totalActivePoints,
    nextExpiry}`` (the "Account Standing" section).

Best-effort and self-contained: any failure returns a summary dict with
``ok=False`` and never raises into the tracker run. Reuses the host/key/HTTP
helpers already in ``buzz_ingest`` to stay on the same request path that works
in production.
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

TABLE = "account_standing_snapshots"
GET_CREATOR_PROC = "user.getCreator"
STRIKE_SUMMARY_PROC = "strike.getMyStrikeSummary"


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

def ensure_account_standing_schema(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at TEXT NOT NULL,
                snapshot_date TEXT NOT NULL UNIQUE,
                follower_count INTEGER,
                reaction_count INTEGER,
                upload_count INTEGER,
                generation_count INTEGER,
                leaderboard_rank INTEGER,
                active_strikes INTEGER,
                total_active_points INTEGER
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
# Parsing (shapes confirmed live; tolerant of missing fields)
# ------------------------------

def parse_creator_stats(creator_node: Dict[str, Any]) -> Dict[str, Optional[int]]:
    """Pull follower/engagement counts + rank out of a user.getCreator payload."""
    stats = creator_node.get("stats") if isinstance(creator_node, dict) else None
    stats = stats if isinstance(stats, dict) else {}
    rank = creator_node.get("rank") if isinstance(creator_node, dict) else None
    rank = rank if isinstance(rank, dict) else {}
    return {
        "follower_count": _safe_int(stats.get("followerCountAllTime")),
        "reaction_count": _safe_int(stats.get("reactionCountAllTime")),
        "upload_count": _safe_int(stats.get("uploadCountAllTime")),
        "generation_count": _safe_int(stats.get("generationCountAllTime")),
        "leaderboard_rank": _safe_int(rank.get("leaderboardRank")),
    }


def parse_strike_summary(strike_node: Dict[str, Any]) -> Dict[str, Optional[int]]:
    node = strike_node if isinstance(strike_node, dict) else {}
    return {
        "active_strikes": _safe_int(node.get("activeStrikes")),
        "total_active_points": _safe_int(node.get("totalActivePoints")),
    }


# ------------------------------
# Fetch
# ------------------------------

def _trpc_get(session: requests.Session, host: str, proc: str, json_input: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    url = make_trpc_url(host, proc, {"json": json_input})
    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()
    # user.getCreator / strike.* return the non-batch {result:{data:{json:...}}}
    # envelope; extract_response_root unwraps the inner `json` payload.
    node = extract_response_root(resp.json())
    return node if isinstance(node, dict) else {}


def fetch_account_standing(
    session: requests.Session,
    host: str,
    username: str,
    timeout: int = 60,
) -> Dict[str, Optional[int]]:
    """Fetch the current follower/standing snapshot fields (no DB write).

    Account-standing strikes are self-scoped and best-effort: if that call
    fails the follower numbers are still returned (strike fields left None).
    """
    creator = _trpc_get(session, host, GET_CREATOR_PROC, {"username": username}, timeout)
    fields: Dict[str, Optional[int]] = dict(parse_creator_stats(creator))
    fields["active_strikes"] = None
    fields["total_active_points"] = None
    try:
        strike = _trpc_get(session, host, STRIKE_SUMMARY_PROC, {"authed": True}, timeout)
        fields.update(parse_strike_summary(strike))
    except Exception:
        pass  # strikes are optional; keep the follower snapshot regardless
    return fields


def write_snapshot(db_path: str, fields: Dict[str, Optional[int]], captured_at: Optional[str] = None) -> bool:
    """Insert one snapshot, deduped to one row per UTC day. Returns True if written."""
    ensure_account_standing_schema(db_path)
    now = utc_now()
    captured = captured_at or iso_z(now)
    snapshot_date = captured[:10]  # YYYY-MM-DD (UTC)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            f"""
            INSERT OR IGNORE INTO {TABLE} (
                captured_at, snapshot_date, follower_count, reaction_count,
                upload_count, generation_count, leaderboard_rank,
                active_strikes, total_active_points
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                captured,
                snapshot_date,
                fields.get("follower_count"),
                fields.get("reaction_count"),
                fields.get("upload_count"),
                fields.get("generation_count"),
                fields.get("leaderboard_rank"),
                fields.get("active_strikes"),
                fields.get("total_active_points"),
            ),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ------------------------------
# Orchestration (called from run_collection_once)
# ------------------------------

def run_follower_ingest(runtime: Dict[str, Any], db_path: str) -> Dict[str, Any]:
    """Fetch + store one follower/standing snapshot. Never raises."""
    summary: Dict[str, Any] = {"ok": False, "written": False, "follower_count": None}
    api_key = runtime.get("api_key")
    username = runtime.get("username")
    if not api_key:
        summary["reason"] = "API key required"
        return summary
    if not username:
        summary["reason"] = "username required"
        return summary

    host = str(runtime.get("view_host") or "https://civitai.red").rstrip("/")
    timeout = int(runtime.get("timeout") or 60)
    try:
        session = requests.Session()
        session.headers.update(_headers(api_key))
        fields = fetch_account_standing(session, host, str(username), timeout)
        written = write_snapshot(db_path, fields)
        summary.update(
            ok=True,
            written=written,
            follower_count=fields.get("follower_count"),
            leaderboard_rank=fields.get("leaderboard_rank"),
            active_strikes=fields.get("active_strikes"),
        )
    except Exception as exc:  # noqa: BLE001
        summary["error"] = str(exc)
    return summary
