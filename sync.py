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

__version__ = "2.3.0"

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
        self.failures_in_a_row = 0
        self.paused_until = 0.0

    def _wait(self):
        wait = self.last_req + self.throttle - time.time()
        if wait > 0:
            time.sleep(wait)
        self.last_req = time.time()

    RETRY_WAITS = (2, 6)       # seconds before the 2nd and 3rd attempt
    TRANSIENT = {500, 502, 503, 504}
    # A POST makes WOM fetch the OSRS hiscores, which can be slow when Jagex is busy.
    TIMEOUTS = {"POST": 30, "GET": 20}

    BREAKER_AFTER = 3          # calls that exhaust their retries in a row ...
    BREAKER_PAUSE = 300        # ... pause WOM calls for this many seconds

    def _request(self, method: str, path: str, params: dict | None = None) -> dict:
        """One WOM call, retried on timeouts / dropped connections / 5xx.

        If WOM is down (several calls in a row fail even after retries), stop calling it for
        a few minutes so a sync finishes quickly and every row keeps its last good values,
        instead of each player waiting out its own timeouts.
        """
        if time.time() < self.paused_until:
            raise WomError("WOM unreachable (paused after repeated failures)")
        try:
            result = self._attempts(method, path, params)
        except WomError as e:
            if str(e).startswith(("WOM unreachable", "WOM 5")):
                self.failures_in_a_row += 1
                if self.failures_in_a_row >= self.BREAKER_AFTER:
                    self.paused_until = time.time() + self.BREAKER_PAUSE
                    log.warning("WOM failing repeatedly; pausing WOM calls for %ds", self.BREAKER_PAUSE)
            raise
        self.failures_in_a_row = 0
        return result

    def _attempts(self, method: str, path: str, params: dict | None) -> dict:
        headers = {"User-Agent": self.user_agent}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        for attempt in range(len(self.RETRY_WAITS) + 1):
            if attempt:
                time.sleep(self.RETRY_WAITS[attempt - 1])
            self._wait()
            try:
                r = requests.request(method, f"{self.BASE}{path}", headers=headers, params=params,
                                     timeout=self.TIMEOUTS.get(method, 20))
            except requests.exceptions.RequestException as e:
                err = WomError(f"WOM unreachable ({type(e).__name__})")
                continue
            if r.status_code in self.TRANSIENT:
                err = WomError(f"WOM {r.status_code}: {error_message(r)}")
                continue
            if r.status_code >= 400:
                raise WomError(f"WOM {r.status_code}: {error_message(r)}")
            return r.json()
        log.info("%s %s: giving up after %d attempts: %s", method, path, attempt + 1, err)
        raise err

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


def error_message(r) -> str:
    """Short, single-line reason from a WOM error response (never a raw HTML page)."""
    try:
        msg = str(r.json().get("message", ""))
    except ValueError:
        msg = "" if r.text.lstrip().startswith("<") else r.text
    msg = " ".join(msg.split()) or (r.reason or "server error")
    return msg[:100]


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


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# Row-5 headers (case-insensitive) for the optional roster-change columns. Keep a
# blank column between the metric block and these so older versions ignore them.
META = ("joined", "lock", "locked at", "synced as")


def read_layout(ws) -> tuple[list[str], dict[str, int]]:
    """Metric keys from D5 (until blank / repeat / meta header) and meta column numbers."""
    header = with_retry(lambda: ws.get(f"{col_letter(FIRST_COL)}{HEADER_ROW}:{HEADER_ROW}"))
    cells = [str(c).strip() for c in (header[0] if header else [])]
    keys = []
    for k in cells:
        if not k or k in keys or k.lower() in META:
            break
        keys.append(k)
    if not keys:
        raise RuntimeError("no metric keys found in Roster header row 5")
    meta = {c.lower(): FIRST_COL + i for i, c in enumerate(cells) if c.lower() in META}
    return keys, (meta if len(meta) == len(META) else {})


def read_keys(ws) -> list[str]:
    return read_layout(ws)[0]


def from_serial(v, tz) -> dt.datetime | None:
    """Sheets date serial -> aware datetime in the sheet's timezone. '' -> None."""
    if v in ("", None):
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"not a date: {v!r}")
    return (dt.datetime(1899, 12, 30) + dt.timedelta(days=v)).replace(tzinfo=tz)


def fetch_player(wom: Wom, name: str, keys: list[str], window: tuple[dt.datetime, dt.datetime],
                 now: dt.datetime) -> tuple[list[int | None], str]:
    """Gains for one player over window = (start, end), plus a status note.

    If WOM can't refresh the player (hiscores slow, WOM hiccup) the gains are still
    read from the snapshots WOM already has, so the row stays current to the last
    good refresh instead of failing outright.

    The fresh snapshot from update() is taken *after* `now`, so the gains window
    is closed at the current time after the update, not at `now`; otherwise every
    sync would report the previous cycle's numbers.
    """
    start, end = window
    note = ""
    if now < end:  # no point refreshing once the window has closed
        try:
            wom.update(name)
        except WomError as e:
            if not str(e).startswith(("WOM unreachable", "WOM 5")):
                raise  # e.g. 400 not on hiscores: a real problem with this player
            note = " (WOM refresh failed, showing last snapshot)"
            log.warning("%s: refresh failed (%s); using existing snapshots", name, e)
    if now < start:
        return [0] * len(keys), note
    stop = min(utcnow(), end)
    if stop <= start:
        return [0] * len(keys), note
    return extract_gains(wom.gained(name, start, stop), keys), note


def guard(key, vals: list, high: dict, name: str = "") -> list:
    """Never report lower gains than already reported for this player + window.

    Gains over a fixed start can only grow, so a lower or missing value means WOM
    returned something incomplete; keep the best value seen instead. Keyed by
    player and window start (not sheet row), so moving or replacing a roster row,
    or a player re-joining another team, never inherits someone else's numbers.
    In-memory: resets on restart.
    """
    best = high.get(key, [None] * len(vals))
    out = []
    for v, b in zip(vals, best):
        if b is not None and (v is None or v < b):
            log.warning("%s: WOM returned %s, keeping %s", name or key, v, b)
            v = b
        out.append(v)
    high[key] = out
    return out


def sync_sheet(gc, sheet_id: str, wom: Wom, cache: dict, window, now: dt.datetime | None = None,
               high: dict | None = None) -> dict:
    sh = with_retry(lambda: gc.open_by_key(sheet_id))
    ws = with_retry(lambda: sh.worksheet(ROSTER_TAB))
    tz = ZoneInfo(sh.fetch_sheet_metadata()["properties"].get("timeZone", "UTC"))
    now = now or utcnow()
    ev_start, ev_end = window
    started = now >= ev_start

    keys, meta = read_layout(ws)
    n = len(keys)
    nrows = LAST_ROW - FIRST_ROW + 1
    rng = lambda c0, c1: f"{col_letter(c0)}{FIRST_ROW}:{col_letter(c1)}{LAST_ROW}"
    raw = lambda r: with_retry(lambda: ws.get(r, value_render_option="UNFORMATTED_VALUE"))
    rows = lambda vals, w: [pad(r, w) for r in vals] + [[""] * w for _ in range(nrows - len(vals))]

    users = with_retry(lambda: ws.get(f"A{FIRST_ROW}:A{LAST_ROW}"))
    names = [pad(r, 1)[0].strip() for r in users] + [""] * (nrows - len(users))
    prev = rows(raw(rng(FIRST_COL, FIRST_COL + n - 1)), n)
    prev_ts = [r[0] for r in rows(raw(f"B{FIRST_ROW}:B{LAST_ROW}"), 1)]
    if meta:
        lo, hi = min(meta.values()), max(meta.values())
        mrows = rows(raw(rng(lo, hi)), hi - lo + 1)
        cell = lambda i, k: mrows[i][meta[k] - lo]

    now_serial = sheet_serial(now.astimezone(tz))
    ser = lambda t: sheet_serial(t.astimezone(tz)) if t else ""
    out, meta_out, joined_out, locked_out, synced_out = [], [], [], [], []
    ok = failed = 0
    for i, name in enumerate(names):
        j_raw = l_raw = s_raw = ""
        if meta:
            j_raw, l_raw, s_raw = cell(i, "joined"), cell(i, "locked at"), str(cell(i, "synced as")).strip()
        if not name:
            out.append([""] * n)
            meta_out.append(["", ""])
            joined_out.append([j_raw]); locked_out.append([l_raw]); synced_out.append([""])
            continue

        note = ""
        joined = locked_at = None
        rstart, rend = ev_start, ev_end
        try:
            if meta:
                joined, locked_at = from_serial(j_raw, tz), from_serial(l_raw, tz)
                lock = cell(i, "lock") is True or str(cell(i, "lock")).upper() == "TRUE"
                if started and joined is None:
                    if s_raw and s_raw.lower() != name.lower():
                        joined = now  # name typed over another player's row
                        note = f" | replaced {s_raw}: their gains were dropped (use Lock + a new row)"
                        log.warning("%s: row was %s; counting %s from now", name, s_raw, name)
                    elif not s_raw and prev_ts[i] in ("", None):
                        joined = now  # brand-new row added after the start
                if not lock:
                    locked_at = None
                elif locked_at is None and started:
                    wom.update(name)  # final snapshot, then close the window after it
                    locked_at = utcnow()
                rstart = max(ev_start, joined) if joined else ev_start
                rend = min(ev_end, locked_at) if locked_at else ev_end
        except ValueError:
            out.append(prev[i]); meta_out.append([prev_ts[i], "Joined / Locked at must be a date and time"])
            joined_out.append([j_raw]); locked_out.append([l_raw]); synced_out.append([s_raw])
            failed += 1
            continue
        except WomError as e:  # lock's final update failed: retry the lock next cycle
            out.append(prev[i]); meta_out.append([prev_ts[i], f"lock pending: {e}"])
            joined_out.append([j_raw]); locked_out.append([l_raw]); synced_out.append([s_raw])
            failed += 1
            continue

        key = (name.lower(), rstart.isoformat(), rend.isoformat())
        if key not in cache:
            try:
                cache[key] = ("ok", *fetch_player(wom, name, keys, (rstart, rend), now))
            except WomError as e:
                cache[key] = ("err", str(e), "")
        state, payload, fetch_note = cache[key]
        if state == "ok" and high is not None:
            payload = guard((name.lower(), rstart.isoformat()), payload, high, name)
            cache[key] = (state, payload, fetch_note)

        joined_out.append([ser(joined)]); locked_out.append([ser(locked_at)])
        if state == "ok":
            unknown = [k for k, v in zip(keys, payload) if v is None]
            status = "locked" if locked_at else "ok"
            if unknown:
                status += f" (unknown metric: {', '.join(unknown)})"
            out.append([("" if v is None else v) for v in payload])
            meta_out.append([now_serial, status + fetch_note + note])
            synced_out.append([name])
            ok += 1
        else:
            out.append(prev[i])  # keep last good gains
            meta_out.append([prev_ts[i], payload])
            synced_out.append([s_raw or ""])
            failed += 1
            log.warning("%s: %s", name, payload)

    summary = f"{ok} ok, {failed} failed"
    if not started:
        summary += f" | event starts {ev_start.astimezone(tz):%a %H:%M}"
    updates = [
        {"range": f"{col_letter(FIRST_COL)}{FIRST_ROW}", "values": out},
        {"range": f"B{FIRST_ROW}", "values": [[m[0]] for m in meta_out]},
        {"range": f"C{FIRST_ROW}", "values": [[m[1]] for m in meta_out]},
        {"range": "B2", "values": [[now_serial, summary]]},
    ]
    if meta:
        updates += [
            {"range": f"{col_letter(meta['joined'])}{FIRST_ROW}", "values": joined_out},
            {"range": f"{col_letter(meta['locked at'])}{FIRST_ROW}", "values": locked_out},
            {"range": f"{col_letter(meta['synced as'])}{FIRST_ROW}", "values": synced_out},
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
