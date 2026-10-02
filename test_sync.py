import datetime as dt

import sync


class FakeWS:
    """Minimal worksheet: dict of A1 cell -> value, supports the calls sync.py makes."""

    def __init__(self, header, users, prev_cur=None, prev_base=None, old_ts=None):
        self.n = len(header)
        self.header, self.users = header, users
        self.prev_cur = prev_cur or []
        self.prev_base = prev_base or []
        self.old_ts = old_ts or []
        self.written = []

    def get(self, rng, value_render_option=None):
        if rng.startswith("D5:"):
            return [self.header]
        if rng.startswith("A7"):
            return [[u] for u in self.users]
        if rng.startswith("B7"):
            return [[t] for t in self.old_ts]
        c0 = sync.CURRENT_FIRST_COL
        if rng.startswith(f"{sync.col_letter(c0)}7"):
            return self.prev_cur
        if rng.startswith(f"{sync.col_letter(c0 + self.n)}7"):
            return self.prev_base
        raise AssertionError(rng)

    def batch_update(self, updates, value_input_option=None):
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


class FakeWom:
    def __init__(self, data):
        self.data, self.calls = data, []

    def player(self, name):
        self.calls.append(name)
        v = self.data[name.lower()]
        if isinstance(v, Exception):
            raise v
        return v


def details(agility=0, mole=0):
    return {"latestSnapshot": {"data": {
        "skills": {"agility": {"experience": agility}, "woodcutting": {"experience": -1}},
        "bosses": {"giant_mole": {"kills": mole}},
    }}}


def test_extract_metrics_skill_boss_unranked_missing():
    out = sync.extract_metrics(details(1234, 7), ["agility", "woodcutting", "giant_mole", "sailing"])
    assert out == [1234, 0, 7, None]


def test_col_letter():
    assert [sync.col_letter(i) for i in (1, 4, 26, 27, 30)] == ["A", "D", "Z", "AA", "AD"]


def run(ws, wom, snapshot=None, cache=None):
    return sync.sync_sheet(FakeGC(ws), "sid", wom, cache if cache is not None else {}, snapshot)


def vals(ws, rng):
    return next(u["values"] for u in ws.written if u["range"].startswith(rng))


def test_sync_writes_current_and_status_and_keeps_failed_values():
    ws = FakeWS(["agility", "giant_mole"], ["Alice", "Bob", "", "Carol"],
                prev_cur=[["", ""], [500, 9], ["", ""], ["", ""]], old_ts=[45000.5, 45000.5, "", ""])
    wom = FakeWom({"alice": details(100, 2), "bob": sync.WomError("not found on WOM/hiscores"),
                   "carol": details(300, 0)})
    res = run(ws, wom)
    assert res == {"ok": 2, "failed": 1}
    cur = vals(ws, "D7")[:4]
    assert cur[0] == [100, 2]
    assert cur[1] == [500, 9]          # failed player keeps last good values
    assert cur[2] == ["", ""]          # empty roster row
    assert cur[3] == [300, 0]
    meta = vals(ws, "B7")[:4]
    assert meta[0][1] == "ok" and isinstance(meta[0][0], float)
    assert meta[1] == [45000.5, "not found on WOM/hiscores"]   # old timestamp preserved
    assert meta[2] == ["", ""]
    assert vals(ws, "B2")[0][1] == "2 ok, 1 failed"
    assert not any(u["range"].startswith("F7") for u in ws.written)  # baseline untouched


def test_baseline_missing_only_fills_blanks_and_skips_failures():
    ws = FakeWS(["agility", "giant_mole"], ["Alice", "Bob", "Carol"],
                prev_cur=[[1, 1], [1, 1], [1, 1]],
                prev_base=[[50, 1], ["", ""], ["", ""]])
    wom = FakeWom({"alice": details(100, 2), "bob": details(200, 3), "carol": sync.WomError("rate limited")})
    run(ws, wom, snapshot="missing")
    base = vals(ws, "F7")[:3]
    assert base[0] == [50, 1]      # existing baseline preserved
    assert base[1] == [200, 3]     # blank filled
    assert base[2] == ["", ""]     # failed player left blank, not zeroed


def test_baseline_force_overwrites_successes_only():
    ws = FakeWS(["agility"], ["Alice", "Bob"], prev_cur=[[1], [1]], prev_base=[[50], [60]])
    wom = FakeWom({"alice": details(100), "bob": sync.WomError("x")})
    run(ws, wom, snapshot="force")
    assert vals(ws, "E7")[:2] == [[100], [60]]


def test_player_fetched_once_across_sheets():
    wom, cache = FakeWom({"alice": details(1)}), {}
    run(FakeWS(["agility"], ["Alice"]), wom, cache=cache)
    run(FakeWS(["agility"], ["alice"]), wom, cache=cache)
    assert wom.calls == ["Alice"]


def test_sheet_serial():
    assert sync.sheet_serial(dt.datetime(1899, 12, 31, 12, 0)) == 1.5