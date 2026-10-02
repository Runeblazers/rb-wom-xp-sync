# rb-wom-xp-sync Architecture

## Overview

**rb-wom-xp-sync** is a lightweight Python service that keeps Google Sheets in sync with player stats from the Wise Old Man (WOM) API. Built for Lucas's OSRS clan bingo event (BSBG), it fetches current XP and boss kill counts and writes them back to each team's tracking sheet.

**Key insight:** The sheet itself holds all the business logic (baselines, gains, team totals, leaderboards) as formulas. The service writes only raw current values; everything else is computed on the sheet.

## Design

### Data flow

Usernames → WOM API (POST→GET) → Extract metrics → Batch write to Sheet

- Sheets provide the UI, formulas, and state
- Service is stateless (no database, no caching across runs)
- Per-run player cache avoids duplication across sheets
- Graceful failure: bad players keep last good values + error status

### Service modes

| Mode | Use case |
|------|----------|
| **Loop** (default) | Production: every INTERVAL_SECONDS (900s default) |
| `--once` | Testing: run once and exit |
| `--snapshot-baseline` | Event start: copy current → baseline |

### Error handling

**Player level:** Failed player keeps last good values, gets error message in status column, loop continues.

**Sheet level:** Failed sheet is logged and alerted; loop continues to next sheet.

**Rate limiting:** Throttles WOM API (0.7s/call with key, 3.2s without). Falls back to GET on POST 429.

## Deployment topology

**Runs on:** lwe-pc (Ubuntu 24.04 LTS, Docker)

- Image: `python:3.12-slim`
- User: `sync` (non-root, UID 10001)
- Restart: `unless-stopped`
- Healthcheck: Every 5 min; fails if no sync in 2.5× interval

**Why lwe-pc, not NAS?**
- Always-on headless (familiar ops)
- Sits alongside Plex/Tdarr (similar pattern)
- NAS reserved for storage

## Dependencies

- `requests>=2.31` — HTTP client
- `gspread>=6.0` — Google Sheets
- `google-auth>=2.29` — OAuth2

All pinned to major versions for stability.

## Testing

7 unit tests (offline, no network, all passing):
- Metric extraction
- Column math
- Sync logic (failures, baselines, deduplication)
- Sheet serial numbers

Run: `pip install pytest && pytest test_sync.py`

## Assumptions (verified/unverified)

✅ WOM endpoint structure  
✅ Sheet metric keys valid  
⚠️ WOM auth header (`x-api-key`) — assumed, not tested with key yet  
⚠️ Rate limits (~100 req/min with key) — assumed, verify live  

Watch first live run for rate-limit messages.

## Monitoring

- `docker compose ps` → health status
- `docker compose logs` → activity
- Healthcheck: `/tmp/xp-sync-heartbeat`
- Discord alerts: configure `DISCORD_WEBHOOK_URL`

---

See DEPLOYMENT.md for setup and REVIEW.md for code assessment.
