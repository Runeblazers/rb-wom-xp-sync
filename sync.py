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

import requests

log = logging.getLogger("xp-sync")

WOM_BASE = os.environ.get("WOM_BASE", "https://api.wiseoldman.net/v2")
ROSTER_TAB = os.environ.get("ROSTER_TAB", "Roster")
HEADER_ROW = 5
FIRST_ROW = 7
LAST_ROW = 56
USER_COL = 1  # A
UPDATED_COL = 2  # B
STATUS_COL = 3  # C
CURRENT_FIRST_COL = 4  # D
HEARTBEAT = Path(os.environ.get("HEARTBEAT_FILE", "/tmp/xp-sync-heartbeat"))
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
EXCEL_EPOCH = dt.datetime(1899, 12, 30)


# ---------------------------------------------------------------- helpers
def col_letter(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def sheet_serial(now_local: dt.datetime) -> float:
    """Google Sheets datetime serial for a naive local datetime."""
    delta = now_local.replace(tzinfo=None) - EXCEL_EPOCH
    return delta.days + delta.seconds / 86400


class WomError(Exception):
    """Per-player failure; message becomes the status cell text."""


# ---------------------------------------------------------------- WOM client
class Wom:
    def __init__(self, api_key: str | None, user_agent: str, delay: float | None = None):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = user_agent
        if api_key:
            self.s.headers["x-api-key"] = api_key
        # Conservative spacing: ~100 req/min with a key, ~20 req/min without.
        self.delay = delay if delay is not None else (0.7 if api_key else 3.2)
        self._last = 0.0

    def _throttle(self):
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def _request(self, method: str, url: str, retries: int = 3):
        for attempt in range(retries + 1):
            self._throttle()
            try:
                r = self.s.request(method, url, timeout=30)
            except requests.RequestException as e:
                if attempt == retries:
                    raise WomError(f"network: {type(e).__name__}") from e
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 500 and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            return r
        raise WomError("unreachable")

    def player(self, username: str) -> dict:
        """Update-then-read a player. Falls back to a plain GET when the update
        is on cooldown / rate limited, so we still get the latest stored snapshot."""
        url = f"{WOM_BASE}/players/{urllib.parse.quote(username, safe='')}"
        r = self._request("POST", url)
        if r.status_code == 429:
            r = self._request("GET", url)
            if r.status_code == 429:
                retry = float(r.headers.get("Retry-After", 20))
                log.warning("rate limited; sleeping %.0fs", retry)
                time.sleep(min(retry, 60))
                r = self._request("GET", url)
        if r.status_code == 404:
            raise WomError("not found on WOM/hiscores")
        if r.status_code == 400:
            raise WomError("rejected (bad username?)")
        if r.status_code in (401, 403):
            raise WomError("auth error (check WOM key)")
        if r.status_code == 429:
            raise WomError("rate limited")
        if not r.ok:
            raise WomError(f"WOM HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise WomError("bad JSON") from e


def extract_metrics(details: dict, keys: list[str]) -> list[int | None]:
    """Pull one value per metric key: skill experience, else boss kills.
    Unranked (-1) becomes 0. Missing metric becomes None (kept as 'unknown')."""
    data = (details.get("latestSnapshot") or {}).get("data") or {}
    skills = data.get("skills") or {}
    bosses = data.get("bosses") or {}
    out: list[int | None] = []
    for k in keys:
        if k in skills:
            v = skills[k].get("experience")
        elif k in bosses:
            v = bosses[k].get("kills")
        else:
            v = None
        out.append(None if v is None else max(0, int(v)))
    return out


# ---------------------------------------------------------------- sheet I/O
def open_gspread(creds_path: str):
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    return gspread.authorize(creds)


def with_retry(fn, tries=4):
    import gspread

    for i in range(tries):
        try:
            return fn()
        except gspread.exceptions.APIError as e:
            code = getattr(getattr(e, "response", None), "status_code", 0)
            if code in (429, 500, 502, 503) and i < tries - 1:
                time.sleep(5 * (i + 1))
                continue
            raise


def pad(row: list, n: int) -> list:
    row = list(row)[:n]
    return row + [""] * (n - len(row))


def sync_sheet(gc, sheet_id: str, wom: Wom, cache: dict, snapshot: str | None) -> dict:
    """snapshot: None | 'missing' (fill blank baselines) | 'force' (overwrite all)."""
    sh = with_retry(lambda: gc.open_by_key(sheet_id))
    ws = with_retry(lambda: sh.worksheet(ROSTER_TAB))
    tz = ZoneInfo(sh.fetch_sheet_metadata()["properties"].get("timeZone", "UTC"))

    header = with_retry(lambda: ws.get(f"{col_letter(CURRENT_FIRST_COL)}{HEADER_ROW}:AZ{HEADER_ROW}"))
    keys = [str(c).strip() for c in (header[0] if header else [])]
    while keys and not keys[-1]:
        keys.pop()
    if not keys:
        raise RuntimeError("no metric keys found in Roster header row")
    n = len(keys)
    cur0, base0 = CURRENT_FIRST_COL, CURRENT_FIRST_COL + n
    nrows = LAST_ROW - FIRST_ROW + 1

    users = with_retry(lambda: ws.get(f"A{FIRST_ROW}:A{LAST_ROW}"))
    names = [pad(r, 1)[0].strip() for r in users] + [""] * (nrows - len(users))
    prev_cur = with_retry(lambda: ws.get(f"{col_letter(cur0)}{FIRST_ROW}:{col_letter(cur0 + n - 1)}{LAST_ROW}"))
    prev_cur = [pad(r, n) for r in prev_cur] + [[""] * n for _ in range(nrows - len(prev_cur))]
    prev_base = with_retry(lambda: ws.get(f"{col_letter(base0)}{FIRST_ROW}:{col_letter(base0 + n - 1)}{LAST_ROW}"))
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
                cache[key] = ("ok", extract_metrics(wom.player(name), keys))
            except WomError as e:
                cache[key] = ("err", str(e))
        state, payload = cache[key]
        if state == "ok":
            vals = [("" if v is None else v) for v in payload]
            row_base = prev_base[i]
            if snapshot == "force" or (snapshot == "missing" and all(b == "" for b in prev_base[i])):
                row_base = vals
            new_cur.append(vals)
            new_base.append(row_base)
            meta.append([now_serial, "ok"])
            ok += 1
        else:
            # keep last good values; surface the error
            new_cur.append(prev_cur[i])
            new_base.append(prev_base[i])
            meta.append(["", payload])  # timestamp restored below
            failed += 1
            log.warning("%s: %s", name, payload)

    # Preserve the old timestamp on failed rows
    old_meta = with_retry(lambda: ws.get(f"B{FIRST_ROW}:B{LAST_ROW}", value_render_option="UNFORMATTED_VALUE"))
    for i, m in enumerate(meta):
        if names[i] and m[1] != "ok":
            m[0] = old_meta[i][0] if i < len(old_meta) and old_meta[i] else ""

    updates = [
        {"range": f"B{FIRST_ROW}:C{LAST_ROW}", "values": meta},
        {"range": f"{col_letter(cur0)}{FIRST_ROW}:{col_letter(cur0 + n - 1)}{LAST_ROW}", "values": new_cur},
        {"range": "B2:C2", "values": [[now_serial, f"{ok} ok, {failed} failed"]]},
    ]
    if snapshot:
        updates.append(
            {"range": f"{col_letter(base0)}{FIRST_ROW}:{col_letter(base0 + n - 1)}{LAST_ROW}", "values": new_base}
        )
    with_retry(lambda: ws.batch_update(updates, value_input_option="RAW"))
    log.info("sheet %s: %d ok, %d failed", sheet_id, ok, failed)
    return {"ok": ok, "failed": failed}


# ---------------------------------------------------------------- orchestration
def read_sheet_ids() -> list[str]:
    env = os.environ.get("SHEET_IDS", "")
    ids = [s.strip() for s in env.split(",") if s.strip()]
    path = Path(os.environ.get("SHEETS_FILE", "/config/sheets.txt"))
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                ids.append(line.split()[0])
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def alert(msg: str):
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return
    stamp = Path("/tmp/xp-sync-last-alert")
    if stamp.exists() and time.time() - stamp.stat().st_mtime < 3600:
        return
    try:
        requests.post(url, json={"content": f"xp-sync: {msg}"[:1900]}, timeout=10)
        stamp.touch()
    except requests.RequestException:
        pass


def run_once(gc, wom: Wom, snapshot: str | None, only: str | None = None) -> bool:
    ids = [only] if only else read_sheet_ids()
    if not ids:
        log.error("no sheet ids configured (SHEETS_FILE or SHEET_IDS)")
        return False
    cache: dict = {}  # a player on several sheets is fetched once per run
    all_ok = True
    for sid in ids:
        try:
            res = sync_sheet(gc, sid, wom, cache, snapshot)
            total = res["ok"] + res["failed"]
            if total and res["failed"] / total > 0.5:
                alert(f"sheet {sid}: {res['failed']}/{total} players failed")
        except Exception as e:  # sheet-level failure (auth, missing tab, ...)
            all_ok = False
            log.exception("sheet %s failed", sid)
            alert(f"sheet {sid} failed: {type(e).__name__}: {e}")
    return all_ok


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--once", action="store_true", help="run a single sync and exit")
    p.add_argument("--snapshot-baseline", action="store_true",
                   help="fill BLANK baselines from current XP (late joiners included), then exit")
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

    if args.snapshot_baseline:
        ok = run_once(gc, wom, "force" if args.force else "missing", args.sheet)
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