> **Historical (v1.x).** Written for the baseline-snapshot design, replaced in v2.0.0 by WOM's `/gained` endpoint. See README.md for current behaviour.

# Handoff: deploy `xp-sync` on the server

You are setting up a small Dockerized Python service on Lucas's always-on server (the one that already runs his clan Discord bot). Everything is written and unit-tested; it has **not yet run against the real WOM API or Google Sheets**. Your job: deploy it, run the first live test, fix anything that differs from the assumptions below, and confirm it loops.

## What it does
Every 15 minutes, for each team's Google Sheet listed in `config/sheets.txt`, it reads usernames from the sheet's `Roster` tab, updates and reads each player from Wise Old Man (WOM), and writes **current** XP/KC back to that tab in one batched write. Baselines, gains, team totals and the Tracker tab numbers are all sheet formulas, so the service writes only raw values. This is for an OSRS clan bingo event ("BSBG"); there is one copy of the spreadsheet per team.

## Files (this folder)
`sync.py` (the service), `healthcheck.py`, `Dockerfile`, `docker-compose.yml`, `requirements.txt`, `.env.example`, `config/sheets.txt`, `test_sync.py` (7 offline tests, all passing), `README.md` (operator docs, read it first).

## Sheet contract (do not change without updating `sync.py` constants)
- Spreadsheet id (team 1, already in `config/sheets.txt`): `1ndltzPH59D2wQjZg5pIXBnk8HZ5DxBbkIuz_QM9UI20`, tab named `Roster`.
- Row 5 holds WOM metric keys starting at D5: `agility, woodcutting, sailing, fishing, runecrafting, hunter, thieving, mining, giant_mole` (9 metrics). The script reads these keys, so metric order/count is driven by the sheet.
- Usernames in `A7:A56`. Script writes `B` (last-updated, as a sheet-timezone datetime serial), `C` (status: `ok` or an error string), and CURRENT block `D:L` (raw values).
- BASELINE block `M:U` is written only by `--snapshot-baseline`. GAINED block `V:AD` and the TEAM TOTAL row 6 are formulas. `B2:C2` = last sync time and summary; a conditional format turns it red if older than 30 minutes.

## Credentials (Lucas provides these; do not ask him to paste them into chat)
- Google service account: `xp-sync@bsbg-sync.iam.gserviceaccount.com`. Needs its **downloaded JSON key file** placed at `./secrets/sa.json` (`chmod 600`). Note: a 40-character hex string Lucas shared earlier is only the key *id*, not the credential; the real credential is the `private_key` inside the JSON file.
- WOM API key: goes in `.env` as `WOM_API_KEY` (copy `.env.example`). Also set `WOM_USER_AGENT` to include a contact handle.
- Each team sheet must be shared with the service account email as **Editor**. Team 1's is already shared.
- `.env` and `secrets/` are git-ignored; keep them out of any repo and out of logs.

## Deploy steps
1. Copy this folder to the server (e.g. next to the bot's compose project, as its own service/project; keep it separate from the bot container).
2. `cp .env.example .env`, fill values; place `secrets/sa.json`.
3. `docker compose build`, then a one-off live test with the username `gingerstork`, which Lucas has already placed in `Roster!A7` of the sheet (lifetime XP will show until a baseline is snapshotted; that is expected). Run `docker compose run --rm xp-sync python sync.py --once`.
4. Check the sheet: `D7:L7` filled, `C7` = `ok`, `B7` timestamp, `B2:C2` updated, and the Tracker tab XP rows (e.g. Agility) reflect the team total from `Roster!V6` etc.
5. Run `docker compose run --rm xp-sync python sync.py --snapshot-baseline` and confirm `M7:U7` fills (only blank baselines; `--force` overwrites all). Then clear the test username/baseline unless Lucas says to keep it.
6. `docker compose up -d`, watch `docker compose logs -f` through one full cycle, confirm `docker ps` shows `healthy` after a successful run.

## Assumptions to verify against live behavior (written from docs/memory, never exercised)
- WOM endpoints: `POST /v2/players/:username` returns PlayerDetails with `latestSnapshot.data.skills.<metric>.experience` and `...bosses.<metric>.kills`. If the update is on cooldown it may return 429 (code falls back to GET).
- Auth/rate limit: API key sent as `x-api-key`; I believe ~100 req/min with a key and ~20 without (the code spaces calls 0.7s/3.2s). Confirm both in WOM's current docs; adjust `Wom.delay` and the header name if different.
- Verified 2026-10-01 via a public unauthenticated `GET /v2/players/gingerstork` (HTTP 200): `skills` contains `agility, woodcutting, sailing, fishing, runecrafting, hunter, thieving, mining` with `experience`, and `bosses` contains `giant_mole` with `kills`. So the sheet's 9 metric keys are valid (including `sailing`). Still unverified: the authenticated `POST` update, the API-key header name, and rate limits.
- `gspread` API usage (`ws.get`, `ws.batch_update(..., value_input_option="RAW")`, `sh.fetch_sheet_metadata()`): tests use fakes, so fix any version drift on the first live run.

## Operations notes
- Add a team: share the new sheet copy with the service account, append its id to `config/sheets.txt`; no rebuild needed. The new copy must have the same `Roster` layout with usernames and baseline cleared.
- Failure behavior: a player who errors keeps their last good value and gets the error in column C (highlighted red); the loop continues. Sheet-level failures are logged and, if `DISCORD_WEBHOOK_URL` is set, posted at most once per hour.
- If a key is ever exposed: delete it in Google Cloud (IAM > Service Accounts > Keys) and create a new one; request a new WOM key.

## Definition of done
One live `--once` run succeeds against the real sheet; baseline snapshot works; container restarts cleanly (`restart: unless-stopped`) and reports healthy; Lucas knows where `.env`, `secrets/` and `config/sheets.txt` live and how to add a team.
