import datetime as dt

import sync

UTC = dt.timezone.utc
START = dt.datetime(2026, 10, 3, 16, 0, tzinfo=UTC)  # 09:00 PDT
WINDOW = (START, dt.datetime.max.replace(tzinfo=UTC))
DURING = START + dt.timedelta(hours=2)


class FakeWS:
    def __init__(self, header, users, prev=None, prev_ts=None):
        self.header, self.users = header, users
        self.prev, self.prev_ts = prev or [], prev_ts or []
        self.written = None

    def get(self, rng, value_render_option=None):
        if rng == "D5:5":
            return [self.header]
        if rng.startswith("A7"):
            return [[u] for u in self.users]
        if rng.startswith("B7"):
            return [[t] for t in self.prev_ts]
        if rng.startswith("D7"):
            return self.prev
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
        return self.data[name.lower()]


def run(ws, wom, now=DURING, cache=None):
    return sync.sync_sheet(FakeGC(ws), "sid", wom, {} if cache is None else cache, WINDOW, now)


def vals(ws, rng):
    return next(u["values"] for u in ws.written if u["range"] == rng)


def test_extract_gains_types_unranked_and_unknown():
    resp = gained(agility=(1000, 5000), mole=(-1, 7), clues=(10, 12))
    out = sync.extract_gains(resp, ["agility", "giant_mole", "clue_scrolls_all", "sailing"])
    assert out == [4000, 7, 2, None]


def test_extract_gains_never_negative_and_unranked_end_is_zero():
    resp = gained(agility=(5000, 4000), mole=(3, -1))
    assert sync.extract_gains(resp, ["agility", "giant_mole"]) == [0, 0]


def test_read_keys_stops_at_repeat_or_blank():
    assert sync.read_keys(FakeWS(["agility", "giant_mole", "agility"], [])) == ["agility", "giant_mole"]
    assert sync.read_keys(FakeWS(["agility", "", "mining"], [])) == ["agility"]


def test_sync_writes_gains_status_and_keeps_failed_values():
    ws = FakeWS(["agility", "giant_mole"], ["Alice", "Bob", "", "Carol"],
                prev=[["", ""], [500, 9], ["", ""], ["", ""]], prev_ts=[45000.5, 45000.5, "", ""])
    wom = FakeWom({"alice": gained((0, 100), (0, 2)), "bob": sync.WomError("WOM 400: not on hiscores"),
                   "carol": gained((10, 310), (1, 1))})
    assert run(ws, wom) == {"ok": 2, "failed": 1}
    rows = vals(ws, "D7")[:4]
    assert rows == [[100, 2], [500, 9], ["", ""], [300, 0]]   # failed keeps last good gains
    meta_ts, meta_st = vals(ws, "B7")[:4], vals(ws, "C7")[:4]
    assert meta_st[0] == ["ok"] and isinstance(meta_ts[0][0], float)
    assert meta_ts[1] == [45000.5] and meta_st[1] == ["WOM 400: not on hiscores"]
    assert vals(ws, "B2")[0][1] == "2 ok, 1 failed"
    assert wom.gains[0][1:] == (START, DURING)


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
    sync.sync_sheet(FakeGC(ws), "sid", wom, {}, (START, end), end + dt.timedelta(hours=3))
    assert wom.updates == [] and wom.gains[0][2] == end


def test_player_fetched_once_across_sheets():
    wom, cache = FakeWom({"alice": gained((0, 1))}), {}
    run(FakeWS(["agility"], ["Alice"]), wom, cache=cache)
    run(FakeWS(["agility"], ["alice"]), wom, cache=cache)
    assert wom.updates == ["Alice"]


def test_next_run_hits_event_start_then_boundaries():
    start = 10_000 * 900  # on a 15-min boundary
    assert sync.next_run(start - 300, 900, start) == start + sync.START_OFFSET_S
    assert sync.next_run(start + 10, 900, start) == start + 900 + sync.START_OFFSET_S
    assert sync.next_run(start - 3000, 900, start) == start - 2700 + sync.START_OFFSET_S


def test_col_letter_and_serial():
    assert [sync.col_letter(i) for i in (1, 4, 26, 27, 30)] == ["A", "D", "Z", "AA", "AD"]
    assert sync.sheet_serial(dt.datetime(1899, 12, 31, 12, 0)) == 1.5


def test_guard_never_lowers_gains_and_fills_missing():
    high = {}
    assert sync.guard("Alice", [100, 5], high) == [100, 5]
    assert sync.guard("alice", [90, None], high) == [100, 5]     # lower / missing -> keep best
    assert sync.guard("Alice", [150, 6], high) == [150, 6]       # growth passes through
    assert sync.guard("Bob", [0, 0], high) == [0, 0]             # per player, not per row


def test_guard_applied_in_sync_and_shared_across_sheets():
    high = {}
    ws1 = FakeWS(["agility"], ["Alice"])
    sync.sync_sheet(FakeGC(ws1), "s1", FakeWom({"alice": gained((0, 500))}), {}, WINDOW, DURING, high)
    ws2 = FakeWS(["agility"], ["Bob", "Alice"])
    sync.sync_sheet(FakeGC(ws2), "s2", FakeWom({"alice": gained((0, 300)), "bob": gained((0, 7))}),
                    {}, WINDOW, DURING, high)
    assert vals(ws2, "D7")[:2] == [[7], [500]]
