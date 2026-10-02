# BSBG xp-sync

Keeps each team's Google Sheet up to date with Wise Old Man (WOM) XP/KC. One container, many sheets.

## How it works
Every `INTERVAL_SECONDS` (default 900) it reads usernames from each sheet's **Roster** tab, calls WOM
(`POST /players/:username` to update + read; falls back to GET on cooldown), and writes **current** XP/KC
back to the Roster tab in one batched write per sheet. Baselines, gains, team totals and the Tracker
numbers are sheet formulas. Failed players keep their last good value and get an error in the Status column.

## Setup
1. Google (once, already done): Cloud project, Sheets API enabled, service account, JSON key.
2. Per team sheet: **Share the sheet with the service account email as Editor**, then add its id to `config/sheets.txt`.
3. On the server:
   ```bash
   cp .env.example .env          # fill WOM_API_KEY, WOM_USER_AGENT
   mkdir -p secrets && cp /path/to/service-account.json secrets/sa.json && chmod 600 secrets/sa.json
   docker compose up -d --build
   docker compose logs -f
   ```
4. At event start, snapshot baselines (fills only blank baselines, so it is safe to re-run for late joiners):
   ```bash
   docker compose run --rm xp-sync python sync.py --snapshot-baseline
   ```
   `--force` overwrites every baseline. `--sheet <id>` limits to one sheet.

## Commands
| Command | Effect |
|---|---|
| `python sync.py` | loop forever (container default) |
| `python sync.py --once` | one sync, then exit (good for a first test) |
| `python sync.py --snapshot-baseline [--force] [--sheet ID]` | copy current -> baseline |

## Adding a team
Copy the sheet, clear the usernames (A7:A56) and baseline block, share the copy with the service account,
add its id to `config/sheets.txt`. The loop picks it up on the next cycle; no rebuild needed.

## Troubleshooting
- `403`/`PERMISSION_DENIED` on a sheet: it is not shared with the service account email.
- `Requested entity was not found`: wrong sheet id, or no tab named `Roster`.
- Status `not found on WOM/hiscores`: username typo or account not on the OSRS hiscores.
- Status `rate limited`: lower request pace via a WOM API key; the script already throttles.
- Container `unhealthy`: no fully successful run within 2.5x the interval; check `docker compose logs`.

## Secrets
`.env` and `secrets/` are git-ignored. If either leaks, delete the service account key in Google Cloud
(IAM > Service Accounts > Keys) and create a new one, and request a new WOM key.

## Tests
`pip install -r requirements.txt pytest && pytest` (no network needed; WOM and Sheets are faked).
