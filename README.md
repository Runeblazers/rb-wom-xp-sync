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

## Hiscores lag (read before the next event)
OSRS hiscores, and so WOM, only see a player's XP when they **log out or hop worlds**. Someone who logs in
before the start and keeps playing has their pre-start XP appear on the hiscores at their next logout/hop, which
counts as gained during the event. On 2026-10-02 two players showed +79k WC / +15k Agility at 09:03–09:05.
The start was moved from 09:00 to **09:10** to exclude those jumps.

For the next event:
- Tell players to **log out or hop before the start** (at 08:55, say). The start-time sync then captures their real XP.
- If an impossible jump still shows up mid-event, type a time just after it into that player's **Joined** cell.
  This drops anything they earned between the start and that time, so use it only for clear dumps.

## Setup
1. Per team sheet: share it with the service account email as **Editor** and add its id to `config/sheets.txt`
   (one per line, `#` comments allowed).
2. On the server:
   ```bash
   cp .env.example .env          # WOM_API_KEY, WOM_USER_AGENT (secrets only)
   # set EVENT_START_DATE / EVENT_END_DATE in docker-compose.yml and commit them
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
| `N7:Q56` | you / script | roster changes, see below |

To add a metric, add its WOM key in row 5 at the end of the block (and a `SUM` in row 6). The script
reads keys until the first blank.

## Roster changes (swaps)
Columns N–Q on Roster (keep column M blank, because older versions read metric keys until the first blank):

| Column | Who | Meaning |
|---|---|---|
| N **Joined** | script, editable | When this player started counting. Blank = event start. |
| O **Lock** | you | Tick to freeze this player's gains. |
| P **Locked at** | script | When the lock took effect. |
| Q **synced as** | script (hidden) | Name last synced in this row, used to spot names typed over. |

- **Swap someone out:** tick **Lock**. The next sync takes one final snapshot, stamps **Locked at**, and freezes
  their gains. The row keeps counting toward the team total. Status shows `locked`. Untick to resume.
- **Add someone:** put them in an empty row. The first sync after the event start that sees them stamps **Joined**
  and counts only from then. To set an exact time, type a date-time into Joined (e.g. `2026-10-05 18:30`).
  Clear Joined to count them from the event start.
- **Don't type a new name over an existing player.** It works, but the old player's gains are dropped from the total.
  The new name counts from that moment and Status says `replaced <old name>`.
- A player moved between teams is tracked separately on each sheet (their own Joined / Locked at window).

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
