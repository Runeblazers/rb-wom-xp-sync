#!/usr/bin/env python3
"""BSBG XP sync: Wise Old Man -> Google Sheets.

Reads usernames from each team sheet's Roster tab, fetches current XP / KC from
the Wise Old Man (WOM) API, and writes the raw current values back to the sheet.
Baselines, gains and team totals are sheet formulas; this script never touches them
except when asked to snapshot baselines.

Roster tab layout (rows are 1-indexed):
  A5 "Username" ... header row 5: D5.. hold WOM metric keys (agility, giant_mole, ...)
  A7:A56            usernames
  B7:B56            last updated (script)   C7:C56  status (script)
  D..               CURRENT block, N columns (one per metric key in row 5)
  next N columns    BASELINE block (script writes only on --snapshot-baseline)
  next N columns    GAINED block (formulas)
  B2                last sync timestamp (script, sheet timezone)  C2  run summary
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import signal
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from zoneinfo import ZoneInfo

import gspread
import requests
from gspread.exceptions import APIError

__version__ = "1.1.1"

ROSTER_TAB = "Roster"
FIRST_ROW, LAST_ROW = 7, 56
HEADER_ROW = 5
CURRENT_FIRST_COL = 4  # 'D' column
HEARTBEAT = Path("/tmp/xp-sync-heartbeat")
log = logging.getLogger("xp-sync")


class WomError(Exception):
    """Raised when a player query fails (404, rate limited, etc)."""
    pass


class Wom:
    """Wise Old Man API client with rate limiting."""
    BASE = "https://api.wiseoldman.net/v2"
    
    def __init__(self, api_key: str | None, user_agent: str):
        self.api_key = api_key
        self.user_agent = user_agent
        self.throttle = 0.7 if api_key else 3.2  # seconds per request
        self.last_req = 0.0
    
    def _throttle(self):
        now = time.time()
        wait = max(0, self.last_req + self.throttle - now)
        if wait > 0:
            time.sleep(wait)
        self.last_req = time.time()
    
    def _request(self, method: str, endpoint: str, **kw) -> dict:
        """POST then GET on 429; raise WomError on failure."""
        self._throttle()
        url = f"{self.BASE}{endpoint}"
        headers = {"User-Agent": self.user_agent}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        try:
            if method == "POST":
                r = requests.post(url, headers=headers, timeout=5, **kw)
                if r.status_code == 429:
                    log.info("%s: rate limited on POST, trying GET", endpoint)
                    self._throttle()
                    r = requests.get(url, headers=headers, timeout=5)
                r.raise_for_status()
            else:
                r = requests.get(url, headers=headers, timeout=5, **kw)
                r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            raise WomError(str(e)) from e
    
    def player(self, username: str) -> dict:
        """GET /players/:username (with POST fallback for updates)."""
        return self._request("POST", f"/players/{urllib.parse.quote(username)}")
    
    def snapshots(self, username: str, start_date: dt.datetime | None = None, end_date: dt.datetime | None = None) -> list[dict]:
        """GET /players/:username/snapshots with optional date range."""
        params = {}
        if start_date:
            params["startDate"] = start_date.isoformat()
        if end_date:
            params["endDate"] = end_date.isoformat()
        return self._request("GET", f"/players/{urllib.parse.quote(username)}/snapshots", params=params).get("data", [])


def extract_metrics(player_data: dict, metric_keys: list[str]) -> list[int | None]:
    """Extract XP/KC for given metric keys from player snapshot data.
    
    Returns list of values, with None for missing metrics.
    Unranked (rank == -1) becomes 0 experience.
    """
    snapshot = player_data.get("latestSnapshot", {}).get("data", {})
    skills = snapshot.get("skills", {})
    bosses = snapshot.get("bosses", {})
    
    vals = []
    for key in metric_keys:
        if key in skills:
            s = skills[key]
            # Unranked (rank == -1) means 0 XP
            val = 0 if s.get("rank", -1) == -1 else s.get("experience")
        elif key in bosses:
            val = bosses[key].get("kills")
        else:
            val = None
        vals.append(val)
    return vals


def extract_metrics_from_snapshot(snapshot_data: dict, metric_keys: list[str]) -> list[int | None]:
    """Extract XP/KC from a single snapshot object (date-stamped).
    
    Snapshot data has same structure as player.latestSnapshot.data.
    """
    skills = snapshot_data.get("skills", {})
    bosses = snapshot_data.get("bosses", {})
    
    vals = []
    for key in metric_keys:
        if key in skills:
            s = skills[key]
            val = 0 if s.get("rank", -1) == -1 else s.get("experience")
        elif key in bosses:
            val = bosses[key].get("kills")
        else:
            val = None
        vals.append(val)
    return vals


def col_letter(col_num: int) -> str:
    """Convert column number (1-indexed) to letter(s): 1='A', 27='AA'."""
    s = ""
    while col_num > 0:
        col_num -= 1
        s = chr(ord("A") + col_num % 26) + s
        col_num //= 26
    return s


def pad(row: list, n: int) -> list:
    """Pad row to length n with empty strings."""
    return row + [""] * max(0, n - len(row))


def with_retry(fn, attempts: int = 3) -> any:
    """Retry gspread API calls on transient errors."""
    for i in range(attempts):
        try:
            return fn()
        except APIError as e:
            if i == attempts - 1 or "429" not in str(e):
                raise
            time.sleep(1 + i)


def sheet_serial(dt_obj: dt.datetime) -> float:
    """Convert datetime to Excel serial number (days since 1900-01-01)."""
    epoch = dt.datetime(1899, 12, 30, tzinfo=dt.timezone.utc)
    delta = dt_obj.replace(tzinfo=None) - epoch.replace(tzinfo=None)
    return delta.total_seconds() / 86400


def open_gspread(creds_path: str) -> gspread.Client:
    """Open gspread client using service account JSON."""
    return gspread.service_account(filename=creds_path)


def alert(msg: str):
    """Post alert to Discord webhook if configured."""
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return
    try:
        requests.post(url, json={"content": f"⚠️ {msg}"}, timeout=5)
    except Exception as e:
        log.warning("discord alert failed: %s", e)


def sync_sheet(gc, sheet_id: str, wom: Wom, cache: dict, snapshot: str | None, baseline_date: dt.datetime | None = None) -> dict:
    """snapshot: None | 'missing' (fill blank baselines) | 'force' (overwrite all).
    baseline_date: if provided, use snapshots from WOM at this date instead of current values.
    """
    sh = with_retry(lambda: gc.open_by_key(sheet_id))
    ws = with_retry(lambda: sh.worksheet(ROSTER_TAB))
    tz = ZoneInfo(sh.fetch_sheet_metadata()["properties"].get("timeZone", "UTC"))

    header = with_retry(lambda: ws.get(f"{col_letter(CURRENT_FIRST_COL)}{HEADER_ROW}:AZ{HEADER_ROW}"))
    keys = []
    for c in (header[0] if header else []):
        k = str(c).strip()
        if not k or k in keys:  # header repeats keys for BASELINE/GAINED blocks; stop there
            break
        keys.append(k)
    if not keys:
        raise RuntimeError("no metric keys found in Roster header row")
    n = len(keys)
    cur0, base0 = CURRENT_FIRST_COL, CURRENT_FIRST_COL + n
    nrows = LAST_ROW - FIRST_ROW + 1

    users = with_retry(lambda: ws.get(f"A{FIRST_ROW}:A{LAST_ROW}"))
    names = [pad(r, 1)[0].strip() for r in users] + [""] * (nrows - len(users))
    prev_cur = with_retry(lambda: ws.get(f"{col_letter(cur0)}{FIRST_ROW}:{col_letter(cur0 + n - 1)}{LAST_ROW}", value_render_option="UNFORMATTED_VALUE"))
    prev_cur = [pad(r, n) for r in prev_cur] + [[""] * n for _ in range(nrows - len(prev_cur))]
    prev_base = with_retry(lambda: ws.get(f"{col_letter(base0)}{FIRST_ROW}:{col_letter(base0 + n - 1)}{LAST_ROW}", value_render_option="UNFORMATTED_VALUE"))
    prev_base = [pad(r, n) for r in prev_base] + [[""] * n for _ in range(nrows - len(prev_base))]

    now_serial = sheet_serial(dt.datetime.now(tz))
    new_cur, new_base, meta = [], [], []
    ok = failed = 0
    for i, name in enumerate(names):
        if not name:
            new_cur.append([""] * n)
            new_base.append(prev_base[i])
            meta.append(["", ""])
            continue
        key = name.lower()
        if key not in cache:
            try:
                # Fetch current data
                current_data = wom.player(name)
                cache[key] = ("ok", extract_metrics(current_data, keys))
                
                # If baseline_date is set, also fetch historical snapshot
                if baseline_date:
                    snapshots = wom.snapshots(name, start_date=baseline_date - dt.timedelta(hours=1), end_date=baseline_date + dt.timedelta(hours=1))
                    if snapshots:
                        # Use the snapshot closest to baseline_date
                        closest = min(snapshots, key=lambda s: abs(dt.datetime.fromisoformat(s["createdAt"].replace("Z", "+00:00")) - baseline_date))
                        baseline_vals = extract_metrics_from_snapshot(closest["data"], keys)
                        cache[f"{key}_baseline"] = ("ok", baseline_vals)
                    else:
                        # No snapshot found for date; fall back to current
                        log.warning("%s: no WOM snapshot near %s, baseline = current XP", name, baseline_date.isoformat())
                        cache[f"{key}_baseline"] = ("ok", extract_metrics(current_data, keys))
            except WomError as e:
                cache[key] = ("err", str(e))
        
        state, payload = cache[key]
        if state == "ok":
            vals = [("" if v is None else v) for v in payload]
            row_base = prev_base[i]
            if snapshot == "force" or (snapshot == "missing" and all(b == "" for b in prev_base[i])):
                # If baseline_date was set, use the historical snapshot values
                if baseline_date:
                    baseline_state, baseline_payload = cache.get(f"{key}_baseline", (None, None))
                    if baseline_state == "ok":
                        row_base = [("" if v is None else v) for v in baseline_payload]
                    else:
                        row_base = vals  # Fall back to current if baseline fetch failed
                else:
                    row_base = vals
            new_cur.append(vals)
            new_base.append(row_base)
            meta.append([now_serial, "ok"])
            ok += 1
        else:
            # keep last good values; surface the error
            new_cur.append(prev_cur[i])
            new_base.append(prev_base[i])
            meta.append(["", payload])
            failed += 1
            log.warning("%s: %s", name, payload)

    updates = [
        {"range": f"{col_letter(cur0)}{FIRST_ROW}", "values": new_cur},
        {"range": f"{col_letter(base0)}{FIRST_ROW}", "values": new_base},
        {"range": f"B{FIRST_ROW}", "values": [[m[0]] for m in meta]},
        {"range": f"C{FIRST_ROW}", "values": [[m[1]] for m in meta]},
        {"range": f"B2", "values": [[now_serial, f"{ok} ok, {failed} failed"]]},
    ]
    with_retry(lambda: ws.batch_update(updates, value_input_option="RAW"))
    return {"ok": ok, "failed": failed}


def run_once(gc, wom: Wom, snapshot: str | None, only: str | None = None, baseline_date: dt.datetime | None = None) -> bool:
    """Run one sync cycle. Returns True if all sheets passed."""
    ids = []
    if only:
        ids = [only]
    else:
        try:
            with open(os.environ.get("SHEETS_FILE", "config/sheets.txt")) as f:
                ids = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        except FileNotFoundError:
            log.error("config/sheets.txt not found")
            return False
    
    all_ok = True
    cache = {}
    for sid in ids:
        try:
            res = sync_sheet(gc, sid, wom, cache, snapshot, baseline_date)
            total = res["ok"] + res["failed"]
            if total and res["failed"] / total > 0.5:
                alert(f"sheet {sid}: {res['failed']}/{total} players failed")
        except Exception as e:
            all_ok = False
            log.exception("sheet %s failed", sid)
            alert(f"sheet {sid} failed: {type(e).__name__}: {e}")
    return all_ok


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--once", action="store_true", help="run a single sync and exit")
    p.add_argument("--snapshot-baseline", action="store_true",
                   help="fill BLANK baselines from current XP (late joiners included), then exit")
    p.add_argument("--baseline-date", type=str, help="snapshot baselines from WOM at this date (ISO 8601, e.g., 2026-10-03T09:00:00-07:00)")
    p.add_argument("--force", action="store_true", help="with --snapshot-baseline: overwrite ALL baselines")
    p.add_argument("--sheet", help="limit to one spreadsheet id")
    args = p.parse_args(argv)

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "/run/secrets/sa.json")
    gc = open_gspread(creds)
    wom = Wom(os.environ.get("WOM_API_KEY") or None,
              os.environ.get("WOM_USER_AGENT", "bsbg-xp-sync (Discord: set WOM_USER_AGENT)"))
    interval = int(os.environ.get("INTERVAL_SECONDS", "900"))

    baseline_date = None
    if args.baseline_date:
        baseline_date = dt.datetime.fromisoformat(args.baseline_date)
        log.info("snapshotting baselines from %s", baseline_date.isoformat())

    if args.snapshot_baseline:
        ok = run_once(gc, wom, "force" if args.force else "missing", args.sheet, baseline_date)
        return 0 if ok else 1
    if args.once:
        return 0 if run_once(gc, wom, None, args.sheet) else 1

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    log.info("starting loop, interval %ss", interval)
    while not stop.is_set():
        started = time.monotonic()
        if run_once(gc, wom, None, args.sheet):
            HEARTBEAT.touch()
        stop.wait(max(5, interval - (time.monotonic() - started)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
