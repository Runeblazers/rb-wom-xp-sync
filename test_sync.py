import datetime as dt
from zoneinfo import ZoneInfo

import pytest

import sync

UTC = dt.timezone.utc
LA = ZoneInfo("America/Los_Angeles")
START = dt.datetime(2026, 10, 3, 16, 0, tzinfo=UTC)  # 09:00 PDT
WINDOW = (START, dt.datetime.max.replace(tzinfo=UTC))
DURING = START + dt.timedelta(hours=2)
CLOCK = {"t": DURING}


@pytest.fixture(autouse=True)
def fake_clock(monkeypatch):
    CLOCK["t"] = DURING
    monkeypatch.setattr(sync, "utcnow", lambda: CLOCK["t"])


META_HDR = ["", "Joined", "Lock", "Locked at", "synced as"]


class FakeWS:
    """Roster with metric header + optional meta columns (after a blank spacer)."""

    def __init__(self, header, users, prev=None, prev_ts=None, meta=None):
        self.header, self.users = header, users
        self.prev, self.prev_ts = prev or [], prev_ts or []
        self.meta = meta  # None = no meta columns; else list of [joined, lock, locked_at, synced_as]
        self.written = None

    @property
    def meta_col(self):  # first meta column letter (after metrics + spacer)
        return sync.col_letter(sync.FIRST_COL + len(self.header) + 1)

    def c(self, name):  # A1 of row 7 for a meta column, e.g. "F7"
        off = ["joined", "lock", "locked at", "synced as"].index(name) + 1
        return f"{sync.col_letter(sync.FIRST_COL + len(self.header) + off)}7"

    def get(self, rng, value_render_option=None):
        if rng == "D5:5":
            return [self.header + (META_HDR if self.meta is not None else [])]
        if rng.startswith("A7"):
            return [[u] for u in self.users]
        if rng.startswith("B7"):
            return [[t] for t in self.prev_ts]
        if rng.startswith("D7"):
            return self.prev
        if self.meta is not None and rng.startswith(f"{self.meta_col}7"):
            return self.meta
        raise AssertionError(rng)

    def batch_update(self, updates, value_input_option=None):
        assert value_input_option == "RAW"
        self.written = updates


class FakeSH:
    def __init__(self, ws):
        self.ws = ws

    def worksheet(self, _):
        return self.ws

    def fetch_sheet_metadata(self):
        return {"properties": {"timeZone": "America/Los_Angeles"}}


class FakeGC:
    def __init__(self, ws):
        self.sh = FakeSH(ws)

    def open_by_key(self, _):
        return self.sh


def g(start, end):
    return {"start": start, "end": end, "gained": end - start}


def gained(agility=(0, 0), mole=(0, 0), clues=None):
    data = {"skills": {"agility": {"experience": g(*agility)}},
            "bosses": {"giant_mole": {"kills": g(*mole)}},
            "activities": {}}
    if clues:
        data["activities"]["clue_scrolls_all"] = {"score": g(*clues)}
    return {"data": data}


class FakeWom:
    def __init__(self, data):
        self.data, self.updates, self.gains = data, [], []

    def update(self, name):
        self.updates.append(name)
        v = self.data[name.lower()]
        if isinstance(v, Exception):
            raise v

    def gained(self, name, start, end):
        self.gains.append((name, start, end))
        v = self.data[name.lower()]
        return v(start, end) if callable(v) else v


def run(ws, wom, now=DURING, cache=None, high=None, window=WINDOW):
    CLOCK["t"] = now
    return sync.sync_sheet(FakeGC(ws), "sid", wom, {} if cache is None else cache, window, now, high)


def vals(ws, rng):
    return next(u["values"] for u in ws.written if u["range"] == rng)


def serial(t):
    return sync.sheet_serial(t.astimezone(LA))


# --- extraction / layout -------------------------------------------------------------

def test_extract_gains_types_unranked_and_unknown():
    resp = gained(agility=(1000, 5000), mole=(-1, 7), clues=(10, 12))
    out = sync.extract_gains(resp, ["agility", "giant_mole", "clue_scrolls_all", "sailing"])
    assert out == [4000, 7, 2, None]


def test_extract_gains_never_negative_and_unranked_end_is_zero():
    resp = gained(agility=(5000, 4000), mole=(3, -1))
    assert sync.extract_gains(resp, ["agility", "giant_mole"]) == [0, 0]


def test_read_layout_stops_at_repeat_blank_or_meta():
    assert sync.read_keys(FakeWS(["agility", "giant_mole", "agility"], [])) == ["agility", "giant_mole"]
    assert sync.read_keys(FakeWS(["agility", "", "mining"], [])) == ["agility"]
    ws = FakeWS(["agility", "mining"], [], meta=[])
    keys, meta = sync.read_layout(ws)
    assert keys == ["agility", "mining"]
    assert meta == {"joined": 7, "lock": 8, "locked at": 9, "synced as": 10}  # D=4, spacer at 6


# --- core sync -----------------------------------------------------------------------

def test_sync_writes_gains_status_and_keeps_failed_values():
    ws = FakeWS(["agility", "giant_mole"], ["Alice", "Bob", "", "Carol"],
                prev=[["", ""], [500, 9], ["", ""], ["", ""]], prev_ts=[45000.5, 45000.5, "", ""])
    wom = FakeWom({"alice": gained((0, 100), (0, 2)), "bob": sync.WomError("WOM 400: not on hiscores"),
                   "carol": gained((10, 310), (1, 1))})
    assert run(ws, wom) == {"ok": 2, "failed": 1}
    assert vals(ws, "D7")[:4] == [[100, 2], [500, 9], ["", ""], [300, 0]]
    ts, st = vals(ws, "B7")[:4], vals(ws, "C7")[:4]
    assert st[0] == ["ok"] and isinstance(ts[0][0], float)
    assert ts[1] == [45000.5] and st[1] == ["WOM 400: not on hiscores"]
    assert vals(ws, "B2")[0][1] == "2 ok, 1 failed"


def test_gains_window_closes_after_the_fresh_snapshot():
    """end of the /gained window is the clock *after* update(), not the sync start time."""
    ws = FakeWS(["agility"], ["Alice"])
    wom = FakeWom({"alice": gained((0, 1))})
    later = DURING + dt.timedelta(seconds=3)
    orig = wom.update
    wom.update = lambda n: (orig(n), CLOCK.__setitem__("t", later))
    sync.sync_sheet(FakeGC(ws), "sid", wom, {}, WINDOW, DURING, None)
    assert wom.gains[0][1:] == (START, later)


def test_before_event_writes_zeros_but_still_validates_names():
    ws = FakeWS(["agility"], ["Alice"])
    wom = FakeWom({"alice": gained((0, 999))})
    run(ws, wom, now=START - dt.timedelta(hours=1))
    assert vals(ws, "D7")[0] == [0]
    assert wom.updates == ["Alice"] and wom.gains == []
    assert "event starts Sat 09:00" in vals(ws, "B2")[0][1]


def test_after_event_end_skips_update_and_clamps_end():
    end = START + dt.timedelta(days=7)
    ws = FakeWS(["agility"], ["Alice"])
    wom = FakeWom({"alice": gained((0, 50))})
    run(ws, wom, now=end + dt.timedelta(hours=3), window=(START, end))
    assert wom.updates == [] and wom.gains[0][2] == end


def test_player_fetched_once_across_sheets():
    wom, cache = FakeWom({"alice": gained((0, 1))}), {}
    run(FakeWS(["agility"], ["Alice"]), wom, cache=cache)
    run(FakeWS(["agility"], ["alice"]), wom, cache=cache)
    assert wom.updates == ["Alice"]


def test_next_run_hits_event_start_then_boundaries():
    start = 10_000 * 900
    assert sync.next_run(start - 300, 900, start) == start + sync.START_OFFSET_S
    assert sync.next_run(start + 10, 900, start) == start + 900 + sync.START_OFFSET_S
    assert sync.next_run(start - 3000, 900, start) == start - 2700 + sync.START_OFFSET_S


def test_col_letter_and_serial():
    assert [sync.col_letter(i) for i in (1, 4, 26, 27, 30)] == ["A", "D", "Z", "AA", "AD"]
    assert sync.sheet_serial(dt.datetime(1899, 12, 31, 12, 0)) == 1.5


# --- never-lower guard ---------------------------------------------------------------

def test_guard_never_lowers_gains_and_fills_missing():
    high = {}
    assert sync.guard("alice", [100, 5], high) == [100, 5]
    assert sync.guard("alice", [90, None], high) == [100, 5]
    assert sync.guard("alice", [150, 6], high) == [150, 6]
    assert sync.guard("bob", [0, 0], high) == [0, 0]


def test_guard_applied_in_sync_and_shared_across_sheets():
    high = {}
    run(FakeWS(["agility"], ["Alice"]), FakeWom({"alice": gained((0, 500))}), high=high)
    ws2 = FakeWS(["agility"], ["Bob", "Alice"])
    run(ws2, FakeWom({"alice": gained((0, 300)), "bob": gained((0, 7))}), high=high)
    assert vals(ws2, "D7")[:2] == [[7], [500]]


# --- roster changes ------------------------------------------------------------------

def meta_row(joined="", lock=False, locked_at="", synced=""):
    return [joined, lock, locked_at, synced]


def window_gains(per_hour):
    """Fake /gained: per_hour XP for each hour of the requested window."""
    def f(start, end):
        xp = int((end - start).total_seconds() / 3600 * per_hour)
        return gained((0, xp))
    return f


def test_original_roster_counts_from_event_start_and_records_synced_name():
    ws = FakeWS(["agility"], ["Alice"], prev_ts=[46000.0], meta=[meta_row()])
    wom = FakeWom({"alice": window_gains(100)})
    run(ws, wom)
    assert wom.gains[0][1] == START
    assert vals(ws, "D7")[0] == [200]                      # 2h * 100
    assert vals(ws, ws.c("joined"))[0] == [""] and vals(ws, ws.c("synced as"))[0] == ["Alice"]


def test_new_row_after_start_is_stamped_joined_and_counts_from_then():
    ws = FakeWS(["agility"], ["Alice", "Newbie"], prev_ts=[46000.0, ""],
                meta=[meta_row(synced="Alice"), meta_row()])
    wom = FakeWom({"alice": window_gains(100), "newbie": window_gains(100)})
    run(ws, wom)
    assert vals(ws, ws.c("joined"))[1] == [serial(DURING)]
    assert "Newbie" in wom.updates                         # first snapshot taken at join
    assert "Newbie" not in [n for n, _, _ in wom.gains]    # zero-length window: nothing to ask
    assert vals(ws, "D7")[1] == [0]                        # just joined
    # next cycle, an hour later: Joined persisted -> counts 1h
    ws2 = FakeWS(["agility"], ["Newbie"], prev_ts=[46000.0],
                 meta=[meta_row(joined=serial(DURING), synced="Newbie")])
    run(ws2, FakeWom({"newbie": window_gains(100)}), now=DURING + dt.timedelta(hours=1))
    assert vals(ws2, "D7")[0] == [100]


def test_new_row_before_start_is_not_stamped():
    ws = FakeWS(["agility"], ["Early"], meta=[meta_row()])
    run(ws, FakeWom({"early": gained()}), now=START - dt.timedelta(minutes=30))
    assert vals(ws, ws.c("joined"))[0] == [""]


def test_name_typed_over_existing_row_is_treated_as_new_and_flagged():
    ws = FakeWS(["agility"], ["Swapped"], prev=[[999]], prev_ts=[46000.0], meta=[meta_row(synced="Alice")])
    wom = FakeWom({"swapped": window_gains(100)})
    run(ws, wom)
    assert vals(ws, ws.c("joined"))[0] == [serial(DURING)]
    assert "replaced Alice" in vals(ws, "C7")[0][0]
    assert vals(ws, ws.c("synced as"))[0] == ["Swapped"]


def test_manual_joined_is_respected():
    joined = START + dt.timedelta(hours=1)
    ws = FakeWS(["agility"], ["Late"], prev_ts=[46000.0], meta=[meta_row(joined=serial(joined), synced="Late")])
    wom = FakeWom({"late": window_gains(100)})
    run(ws, wom)
    assert wom.gains[0][1] == joined.astimezone(LA)
    assert vals(ws, "D7")[0] == [100]


def test_lock_takes_final_snapshot_then_freezes():
    lock_time = DURING + dt.timedelta(seconds=2)
    ws = FakeWS(["agility"], ["Alice"], prev_ts=[46000.0], meta=[meta_row(lock=True, synced="Alice")])
    wom = FakeWom({"alice": window_gains(100)})
    orig = wom.update
    wom.update = lambda n: (orig(n), CLOCK.__setitem__("t", lock_time))
    sync.sync_sheet(FakeGC(ws), "sid", wom, {}, WINDOW, DURING, {})
    assert wom.updates[0] == "Alice"
    assert vals(ws, ws.c("locked at"))[0] == [serial(lock_time)]
    assert vals(ws, "C7")[0] == ["locked"]
    assert wom.gains[-1][2] == lock_time
    # later cycles: no updates, window stays closed at lock time
    ws2 = FakeWS(["agility"], ["Alice"], prev_ts=[46000.0],
                 meta=[meta_row(lock=True, locked_at=serial(lock_time), synced="Alice")])
    wom2 = FakeWom({"alice": window_gains(100)})
    run(ws2, wom2, now=DURING + dt.timedelta(hours=5))
    assert wom2.updates == []
    assert vals(ws2, "D7")[0] == [200]


def test_unlock_clears_locked_at_and_resumes():
    lock_time = DURING - dt.timedelta(hours=1)
    ws = FakeWS(["agility"], ["Alice"], prev_ts=[46000.0],
                meta=[meta_row(lock=False, locked_at=serial(lock_time), synced="Alice")])
    run(ws, FakeWom({"alice": window_gains(100)}))
    assert vals(ws, ws.c("locked at"))[0] == [""] and vals(ws, "D7")[0] == [200]


def test_lock_before_start_does_nothing_yet():
    ws = FakeWS(["agility"], ["Alice"], meta=[meta_row(lock=True)])
    run(ws, FakeWom({"alice": gained()}), now=START - dt.timedelta(minutes=5))
    assert vals(ws, ws.c("locked at"))[0] == [""]


def test_failed_lock_snapshot_retries_next_cycle():
    ws = FakeWS(["agility"], ["Alice"], prev=[[50]], prev_ts=[46000.0], meta=[meta_row(lock=True, synced="Alice")])
    run(ws, FakeWom({"alice": sync.WomError("WOM 500: down")}))
    assert vals(ws, ws.c("locked at"))[0] == [""] and vals(ws, "D7")[0] == [50]
    assert vals(ws, "C7")[0][0].startswith("lock pending")


def test_bad_date_in_meta_keeps_values_and_reports():
    ws = FakeWS(["agility"], ["Alice"], prev=[[70]], prev_ts=[46000.0],
                meta=[meta_row(joined="tomorrow", synced="Alice")])
    run(ws, FakeWom({"alice": gained((0, 1))}))
    assert vals(ws, "D7")[0] == [70] and vals(ws, ws.c("joined"))[0] == ["tomorrow"]
    assert "must be a date" in vals(ws, "C7")[0][0]


def test_same_player_on_two_teams_with_different_windows_not_shared():
    """Locked on team A, re-added to team B later: separate cache + guard entries."""
    high, cache = {}, {}
    lock_at = START + dt.timedelta(hours=1)
    a = FakeWS(["agility"], ["Mover"], prev_ts=[46000.0],
               meta=[meta_row(lock=True, locked_at=serial(lock_at), synced="Mover")])
    b = FakeWS(["agility"], ["Mover"], prev_ts=[46000.0],
               meta=[meta_row(joined=serial(lock_at), synced="Mover")])
    wom = FakeWom({"mover": window_gains(100)})
    run(a, wom, cache=cache, high=high)
    run(b, wom, cache=cache, high=high)
    assert vals(a, "D7")[0] == [100]   # 09:00 -> 10:00
    assert vals(b, "D7")[0] == [100]   # 10:00 -> 11:00


def test_sheet_without_meta_columns_still_works():
    ws = FakeWS(["agility"], ["Alice"])
    run(ws, FakeWom({"alice": gained((0, 5))}))
    assert vals(ws, "D7")[0] == [5]
    assert len(ws.written) == 4  # gains, B, C, B2 only


# --- WOM resilience ------------------------------------------------------------------

class FakeResp:
    def __init__(self, status, body=None, text="", reason="Bad Gateway"):
        self.status_code, self._body, self.text, self.reason = status, body, text, reason

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


HTML_502 = '<!DOCTYPE html>\n<!--[if lt IE 7]> <html class="no-js ie6 oldie" lang="en-US"> <![endif]-->'


def test_error_message_never_dumps_html():
    assert sync.error_message(FakeResp(502, text=HTML_502)) == "Bad Gateway"
    assert sync.error_message(FakeResp(400, body={"message": "Invalid username"})) == "Invalid username"
    assert "\n" not in sync.error_message(FakeResp(500, text="line one\nline two"))


def _wom_with(monkeypatch, responses):
    calls = []
    def fake_request(method, url, **kw):
        calls.append((method, kw["timeout"]))
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(sync.requests, "request", fake_request)
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)
    return sync.Wom("key", "test"), calls


def test_request_retries_transient_failures_then_succeeds(monkeypatch):
    wom, calls = _wom_with(monkeypatch, [
        sync.requests.exceptions.ReadTimeout(), FakeResp(502, text=HTML_502), FakeResp(200, body={"ok": 1})])
    assert wom._request("POST", "/players/x") == {"ok": 1}
    assert calls == [("POST", 30)] * 3


def test_request_gives_up_with_a_short_message(monkeypatch):
    wom, calls = _wom_with(monkeypatch, [sync.requests.exceptions.ReadTimeout()] * 3)
    with pytest.raises(sync.WomError, match=r"^WOM unreachable \(ReadTimeout\)$"):
        wom._request("GET", "/players/x/gained")
    assert len(calls) == 3


def test_request_does_not_retry_client_errors(monkeypatch):
    wom, calls = _wom_with(monkeypatch, [FakeResp(400, body={"message": "Invalid username"})])
    with pytest.raises(sync.WomError, match="WOM 400: Invalid username"):
        wom._request("POST", "/players/x")
    assert len(calls) == 1


def test_refresh_outage_still_reports_gains_from_existing_snapshots():
    ws = FakeWS(["agility"], ["Alice", "Bob"], prev=[[5], [9]], prev_ts=[46000.0, 46000.0])
    wom = FakeWom({"alice": gained((0, 40)), "bob": gained((0, 50))})
    def flaky_update(name):
        wom.updates.append(name)
        if name == "Alice":
            raise sync.WomError("WOM unreachable (ReadTimeout)")
    wom.update = flaky_update
    assert run(ws, wom) == {"ok": 2, "failed": 0}
    assert vals(ws, "D7")[:2] == [[40], [50]]
    assert vals(ws, "C7")[0][0] == "ok (WOM refresh failed, showing last snapshot)"
    assert vals(ws, "C7")[1] == ["ok"]


def test_gained_outage_keeps_last_good_values():
    ws = FakeWS(["agility"], ["Alice"], prev=[[77]], prev_ts=[46000.5])
    wom = FakeWom({"alice": gained((0, 1))})
    def down(*a):
        raise sync.WomError("WOM 502: Bad Gateway")
    wom.gained = down
    assert run(ws, wom) == {"ok": 0, "failed": 1}
    assert vals(ws, "D7")[0] == [77] and vals(ws, "B7")[0] == [46000.5]
    assert vals(ws, "C7")[0] == ["WOM 502: Bad Gateway"]


def test_breaker_stops_calling_wom_after_repeated_failures(monkeypatch):
    wom, calls = _wom_with(monkeypatch, [sync.requests.exceptions.ConnectTimeout()] * 9)
    clock = {"t": 1000.0}
    monkeypatch.setattr(sync.time, "time", lambda: clock["t"])
    for _ in range(3):
        with pytest.raises(sync.WomError):
            wom._request("GET", "/x")
    assert len(calls) == 9
    with pytest.raises(sync.WomError, match="paused"):
        wom._request("GET", "/x")
    assert len(calls) == 9                       # no network while paused
    clock["t"] += sync.Wom.BREAKER_PAUSE + 1
    monkeypatch.setattr(sync.requests, "request", lambda *a, **k: FakeResp(200, body={"ok": 1}))
    assert wom._request("GET", "/x") == {"ok": 1}
    assert wom.failures_in_a_row == 0


def test_client_errors_do_not_trip_the_breaker(monkeypatch):
    wom, _ = _wom_with(monkeypatch, [FakeResp(400, body={"message": "Invalid username"})] * 5)
    for _ in range(5):
        with pytest.raises(sync.WomError):
            wom._request("POST", "/players/typo")
    assert wom.paused_until == 0.0
