#!/usr/bin/env python3
"""BSBG XP sync: Wise Old Man -> Google Sheets.

Reads usernames from each team sheet's Roster tab, asks Wise Old Man (WOM) for
each player's XP / KC *gained* between EVENT_START_DATE and now (or
EVENT_END_DATE), and writes those gains straight into the sheet. No baselines,
no subtraction in the sheet: WOM's /gained endpoint does the maths.

Roster tab layout (rows are 1-indexed):
  row 5, D5..       WOM metric keys, one per column (agility, giant_mole, ...)
  A7:A56            usernames (entered by hand)
  B7:B56            last updated (script)    C7:C56  status (script)
  D7..              GAINED since event start, one column per metric key (script)
  row 6             TEAM TOTAL =SUM() formulas (sheet)
  B2 / C2           last sync timestamp / run summary (script)

Timing: every sync first POSTs /players/:name, which makes WOM take a fresh
snapshot. WOM computes gains from the first to the last snapshot *inside* the
window, so a sync is scheduled right at EVENT_START_DATE (and then on wall-clock
multiples of INTERVAL_SECONDS) to put each player's first in-window snapshot as
close to the start as possible.
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

__version__ = "2.1.0"

ROSTER_TAB = "Roster"
FIRST_ROW, LAST_ROW = 7, 56
HEADER_ROW = 5
FIRST_COL = 4  # 'D'
START_OFFSET_S = 5  # run this many seconds after a boundary so snapshots land inside the window
HEARTBEAT = Path("/tmp/xp-sync-heartbeat")
log = logging.getLogger("xp-sync")


class WomError(Exception):
    """A per-player WOM failure (not on hiscores, rate limited, network)."""


class Wom:
    """Wise Old Man v2 client with simple client-side throttling."""
    BASE = "https://api.wiseoldman.net/v2"

    def __init__(self, api_key: str | None, user_agent: str):
        self.api_key = api_key
        self.user_agent = user_agent
        self.throttle = 0.7 if api_key else 3.2  # seconds between requests
        self.last_req = 0.0

    def _wait(self):
        wait = self.last_req + self.throttle - time.time()
        if wait > 0:
            time.sleep(wait)
        self.last_req = time.time()

    def _request(self, method: str, path: str, params: dict | None = None) -> dict:
        self._wait()
        headers = {"User-Agent": self.user_agent}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        try:
            r = requests.request(method, f"{self.BASE}{path}", headers=headers, params=params, timeout=15)
        except requests.exceptions.RequestException as e:
            raise WomError(f"network: {type(e).__name__}") from e
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", "")
            except ValueError:
                msg = r.text[:120]
            raise WomError(f"WOM {r.status_code}: {msg}".strip())
        return r.json()

    def update(self, username: str) -> None:
        """POST /players/:username -> WOM fetches hiscores and stores a snapshot.

        A 429 here usually means 'updated very recently' — the existing snapshot is
        fresh enough, so it is not an error for our purposes.
        """
        try:
            self._request("POST", f"/players/{urllib.parse.quote(username)}")
        except WomError as e:
            if "429" in str(e):
                log.debug("%s: update cooldown, using existing snapshots", username)
                return
            raise

    def gained(self, username: str, start: dt.datetime, end: dt.datetime) -> dict:
        """GET /players/:username/gained for an explicit date range."""
        params = {"startDate": iso_utc(start), "endDate": iso_utc(end)}
        return self._request("GET", f"/players/{urllib.parse.quote(username)}/gained", params=params)


def iso_utc(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# WOM groups metrics by type; each type stores its measure under a different key.
MEASURE = {"skills": "experience", "bosses": "kills", "activities": "score", "computed": "value"}


def extract_gains(gained: dict, keys: list[str]) -> list[int | None]:
    """Gains per metric key from a /gained response. None = key unknown to WOM.

    WOM reports unranked hiscore entries as -1 (e.g. boss KC below the hiscore
    threshold). Treat -1 as 0 so a player who goes unranked -> ranked during the
    event isn't credited with a phantom +1, and never report negative gains.
    """
    data = gained.get("data", {})
    out = []
    for key in keys:
        val = None
        for group, measure in MEASURE.items():
            entry = data.get(group, {}).get(key)
            if entry is None:
                continue
            m = entry.get(measure) or {}
            start, end = m.get("start"), m.get("end")
            if end is None or end < 0:
                val = 0
            else:
                val = max(0, round(end - max(start or 0, 0)))
            break
        out.append(val)
    return out


def col_letter(n: int) -> str:
    """1 -> 'A', 27 -> 'AA'."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def pad(row: list, n: int) -> list:
    return row + [""] * max(0, n - len(row))


def with_retry(fn, attempts: int = 4):
    """Retry Sheets calls on 429 / 5xx with backoff."""
    for i in range(attempts):
        try:
            return fn()
        except APIError as e:
            code = getattr(getattr(e, "response", None), "status_code", 0)
            if i == attempts - 1 or not (code == 429 or code >= 500):
                raise
            time.sleep(2 ** i)


def sheet_serial(t: dt.datetime) -> float:
    """Sheets date serial (days since 1899-12-30), local wall time of t."""
    return (t.replace(tzinfo=None) - dt.datetime(1899, 12, 30)).total_seconds() / 86400


def read_keys(ws) -> list[str]:
    header = with_retry(lambda: ws.get(f"{col_letter(FIRST_COL)}{HEADER_ROW}:{HEADER_ROW}"))
    keys = []
    for c in (header[0] if header else []):
        k = str(c).strip()
        if not k or k in keys:
            break
        keys.append(k)
    if not keys:
        raise RuntimeError("no metric keys found in Roster header row 5")
    return keys


def fetch_player(wom: Wom, name: str, keys: list[str], window: tuple[dt.datetime, dt.datetime],
                 now: dt.datetime) -> list[int | None]:
    start, end = window
    if now < end:  # no point refreshing after the event is over
        wom.update(name)
    if now < start:
        return [0] * len(keys)
    return extract_gains(wom.gained(name, start, min(now, end)), keys)


def guard(name: str, vals: list, high: dict) -> list:
    """Never report lower gains than already reported for this player.

    Gains over a fixed window can only grow, so a lower value or a missing one
    means WOM returned something incomplete; keep the best value seen instead.
    Keyed by player name (not sheet row) so moving or replacing a roster row
    never hands one player's gains to another. In-memory: resets on restart.
    """
    best = high.get(name.lower(), [None] * len(vals))
    out = []
    for v, b in zip(vals, best):
        if b is not None and (v is None or v < b):
            log.warning("%s: WOM returned %s, keeping %s", name, v, b)
            v = b
        out.append(v)
    high[name.lower()] = out
    return out


def sync_sheet(gc, sheet_id: str, wom: Wom, cache: dict, window, now: dt.datetime | None = None,
               high: dict | None = None) -> dict:
    sh = with_retry(lambda: gc.open_by_key(sheet_id))
    ws = with_retry(lambda: sh.worksheet(ROSTER_TAB))
    tz = ZoneInfo(sh.fetch_sheet_metadata()["properties"].get("timeZone", "UTC"))
    now = now or dt.datetime.now(dt.timezone.utc)

    keys = read_keys(ws)
    n = len(keys)
    nrows = LAST_ROW - FIRST_ROW + 1
    last_col = col_letter(FIRST_COL + n - 1)

    users = with_retry(lambda: ws.get(f"A{FIRST_ROW}:A{LAST_ROW}"))
    names = [pad(r, 1)[0].strip() for r in users] + [""] * (nrows - len(users))
    prev = with_retry(lambda: ws.get(f"{col_letter(FIRST_COL)}{FIRST_ROW}:{last_col}{LAST_ROW}",
                                     value_render_option="UNFORMATTED_VALUE"))
    prev = [pad(r, n) for r in prev] + [[""] * n for _ in range(nrows - len(prev))]
    prev_ts = with_retry(lambda: ws.get(f"B{FIRST_ROW}:B{LAST_ROW}", value_render_option="UNFORMATTED_VALUE"))
    prev_ts = [pad(r, 1)[0] for r in prev_ts] + [""] * (nrows - len(prev_ts))

    now_serial = sheet_serial(now.astimezone(tz))
    out, meta = [], []
    ok = failed = 0
    for i, name in enumerate(names):
        if not name:
            out.append([""] * n)
            meta.append(["", ""])
            continue
        key = name.lower()
        if key not in cache:
            try:
                cache[key] = ("ok", fetch_player(wom, name, keys, window, now))
            except WomError as e:
                cache[key] = ("err", str(e))
        state, payload = cache[key]
        if state == "ok" and high is not None:
            payload = guard(name, payload, high)
            cache[key] = (state, payload)
        if state == "ok":
            unknown = [k for k, v in zip(keys, payload) if v is None]
            out.append([("" if v is None else v) for v in payload])
            meta.append([now_serial, f"ok (unknown metric: {', '.join(unknown)})" if unknown else "ok"])
            ok += 1
        else:
            out.append(prev[i])  # keep last good gains
            meta.append([prev_ts[i], payload])
            failed += 1
            log.warning("%s: %s", name, payload)

    start, _ = window
    summary = f"{ok} ok, {failed} failed"
    if now < start:
        summary += f" | event starts {start.astimezone(tz):%a %H:%M}"
    updates = [
        {"range": f"{col_letter(FIRST_COL)}{FIRST_ROW}", "values": out},
        {"range": f"B{FIRST_ROW}", "values": [[m[0]] for m in meta]},
        {"range": f"C{FIRST_ROW}", "values": [[m[1]] for m in meta]},
        {"range": "B2", "values": [[now_serial, summary]]},
    ]
    with_retry(lambda: ws.batch_update(updates, value_input_option="RAW"))
    return {"ok": ok, "failed": failed}


def alert(msg: str):
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return
    try:
        requests.post(url, json={"content": f"⚠️ xp-sync: {msg}"}, timeout=5)
    except Exception as e:  # never let alerting break a sync
        log.warning("discord alert failed: %s", e)


def load_sheet_ids(only: str | None) -> list[str]:
    if only:
        return [only]
    with open(os.environ.get("SHEETS_FILE", "config/sheets.txt")) as f:
        return [ln.split("#")[0].strip() for ln in f if ln.split("#")[0].strip()]


def run_once(gc, wom: Wom, window, only: str | None = None, high: dict | None = None) -> bool:
    try:
        ids = load_sheet_ids(only)
    except FileNotFoundError:
        log.error("sheets file not found")
        return False
    all_ok, cache = True, {}
    for sid in ids:
        try:
            res = sync_sheet(gc, sid, wom, cache, window, high=high)
            log.info("sheet %s: %d ok, %d failed", sid, res["ok"], res["failed"])
            total = res["ok"] + res["failed"]
            if total and res["failed"] / total > 0.5:
                alert(f"sheet {sid}: {res['failed']}/{total} players failed")
        except Exception as e:
            all_ok = False
            log.exception("sheet %s failed", sid)
            alert(f"sheet {sid} failed: {type(e).__name__}: {e}")
    return all_ok


def next_run(now_ts: float, interval: int, start_ts: float) -> float:
    """Next wall-clock multiple of interval (+offset); the event start itself if it comes first."""
    boundary = (now_ts // interval + 1) * interval + START_OFFSET_S
    first = start_ts + START_OFFSET_S
    return first if now_ts < first < boundary else boundary


def parse_window() -> tuple[dt.datetime, dt.datetime]:
    raw_start = os.environ.get("EVENT_START_DATE")
    if not raw_start:
        raise SystemExit("EVENT_START_DATE is required (ISO 8601, e.g. 2026-10-03T09:00:00-07:00)")
    start = dt.datetime.fromisoformat(raw_start)
    raw_end = os.environ.get("EVENT_END_DATE")
    end = dt.datetime.fromisoformat(raw_end) if raw_end else dt.datetime.max.replace(tzinfo=dt.timezone.utc)
    if start.tzinfo is None or end.tzinfo is None:
        raise SystemExit("EVENT_START_DATE / EVENT_END_DATE must include a UTC offset")
    if end <= start:
        raise SystemExit("EVENT_END_DATE must be after EVENT_START_DATE")
    return start, end


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--once", action="store_true", help="run a single sync and exit")
    p.add_argument("--sheet", help="limit to one spreadsheet id")
    args = p.parse_args(argv)

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    window = parse_window()
    gc = gspread.service_account(filename=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "/run/secrets/sa.json"))
    wom = Wom(os.environ.get("WOM_API_KEY") or None,
              os.environ.get("WOM_USER_AGENT", "bsbg-xp-sync (set WOM_USER_AGENT)"))
    interval = int(os.environ.get("INTERVAL_SECONDS", "900"))
    log.info("xp-sync %s, window %s -> %s", __version__, window[0].isoformat(),
             "open" if window[1].year == dt.MAXYEAR else window[1].isoformat())

    if args.once:
        return 0 if run_once(gc, wom, window, args.sheet) else 1

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    start_ts = window[0].timestamp()
    high: dict = {}  # best gains reported per player this process
    while not stop.is_set():
        if run_once(gc, wom, window, args.sheet, high):
            HEARTBEAT.touch()
        wake = next_run(time.time(), interval, start_ts)
        log.info("next sync at %s", dt.datetime.fromtimestamp(wake).astimezone().strftime("%H:%M:%S"))
        stop.wait(max(5, wake - time.time()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
