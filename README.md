# BSBG xp-sync

Writes each player's **XP / KC gained since the event started** into every team's Google Sheet,
using Wise Old Man (WOM). One container, many sheets.

## How it works
Every `INTERVAL_SECONDS` (default 900, aligned to the wall clock: :00, :15, :30, :45) it:
1. reads usernames from each sheet's **Roster** tab (`A7:A56`) and the metric keys from row 5 (`D5…`),
2. calls `POST /players/:username` so WOM takes a fresh snapshot,
3. calls `GET /players/:username/gained?startDate=EVENT_START_DATE&endDate=now` and writes the gains
   into `D7:L56`, plus a timestamp (B) and status (C), in one batched write per sheet.

No baselines and no subtraction in the sheet. Row 6 `TEAM TOTAL` sums the columns and the Tracker tab
reads `Roster!D6:L6`. Failed players keep their last good gains and show the error in Status.
A player's gains never go down: if WOM returns a lower or missing value, the best value seen so far
is kept and a warning is logged. This is tracked per player name, in memory, and resets when the container restarts.

**Timing.** WOM measures gains between the first and last snapshot *inside* the window, so the service
schedules a sync for `EVENT_START_DATE + 5s`. Each player's first counted snapshot is taken within about a
minute of the start. Keep the container running across the start time. Before the start the sheet
shows 0 and C2 says when the event begins. If `EVENT_END_DATE` is set, gains freeze at that time.

**Edge case.** Boss KC below the hiscores threshold is reported as unranked by WOM. A player going from
unranked to ranked during the event gets credit for their full KC at that point (e.g. 0→5 even if they
had 3 before). Minor, and the same as WOM's own competitions.

## Setup
1. Per team sheet: share it with the service account email as **Editor** and add its id to `config/sheets.txt`
   (one per line, `#` comments allowed).
2. On the server:
   ```bash
   cp .env.example .env          # WOM_API_KEY, WOM_USER_AGENT, EVENT_START_DATE (+ optional EVENT_END_DATE)
   mkdir -p secrets config && cp /path/to/service-account.json secrets/sa.json && chmod 600 secrets/sa.json
   docker compose up -d --build
   docker compose logs -f
   ```

## Commands
| Command | Effect |
|---|---|
| `python sync.py` | loop forever (container default) |
| `python sync.py --once [--sheet ID]` | one sync, then exit |

## Roster layout
| Cells | Owner | Content |
|---|---|---|
| `A7:A56` | you | usernames |
| `D5:L5` | you | WOM metric keys (`agility`, `giant_mole`, `clue_scrolls_all`, …) |
| `B7:C56`, `D7:L56`, `B2:C2` | script | updated / status / gains / last sync |
| `D6:L6` | sheet | `=SUM()` team totals (Tracker reads these) |

To add a metric, add its WOM key in row 5 at the end of the block (and a `SUM` in row 6). The script
reads keys until the first blank.

## Adding a team
Copy BSBG_MASTER, put usernames in A7:A56, share with the service account, and add the id to
`config/sheets.txt`. The next cycle picks it up with no rebuild.

## Troubleshooting
- `403`/`PERMISSION_DENIED`: sheet not shared with the service account.
- `Requested entity was not found`: wrong sheet id, or no `Roster` tab.
- Status `WOM 400 …`: username typo or not on the OSRS hiscores.
- Status `ok (unknown metric: x)`: row-5 key isn't a WOM metric name.
- Container `unhealthy`: no fully successful run within 2.5× the interval. Check `docker compose logs`.

## Tests
`pip install -r requirements.txt pytest && pytest` (offline; WOM and Sheets are faked).
