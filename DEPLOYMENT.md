# rb-wom-xp-sync Deployment Guide

Deploy xp-sync to lwe-pc as a background service that updates BSBG team sheets every 15 minutes.

## Prerequisites

- lwe-pc SSH access (key-based auth)
- Google Cloud service account with Sheets API (JSON key file)
- Service account shared as Editor on each team sheet
- Wise Old Man API key (optional but recommended)
- Docker and Docker Compose on lwe-pc (already installed)

## Quick start

```bash
# On lwe-pc
ssh lwe-pc
mkdir -p /home/lwe/docker/xp-sync
cd /home/lwe/docker/xp-sync

# Clone repo (or copy files)
git clone ssh://git@192.168.1.247:222/lwe/rb-wom-xp-sync.git .

# Copy secrets from Mac
scp <from Mac> lwe@192.168.1.247:/home/lwe/docker/xp-sync/secrets/sa.json
chmod 600 /home/lwe/docker/xp-sync/secrets/sa.json

# Create .env
cp .env.example .env
nano .env
# Set: WOM_API_KEY, WOM_USER_AGENT, optionally DISCORD_WEBHOOK_URL

# Create config
mkdir -p config
echo "1ndltzPH59D2wQjZg5pIXBnk8HZ5DxBbkIuz_QM9UI20" > config/sheets.txt

# Build and test
docker compose build
docker compose run --rm xp-sync python sync.py --once
# Check the sheet manually — before the event D7:L7 show 0 and C2 says when it starts

# Start the service
docker compose up -d
docker compose ps  # should show healthy after ~5 min
```

## Detailed steps

### 1. Prepare directory on lwe-pc

```bash
ssh lwe-pc
mkdir -p /home/lwe/docker/xp-sync/secrets /home/lwe/docker/xp-sync/config
cd /home/lwe/docker/xp-sync
```

### 2. Clone the repo or copy files

If Gitea is set up:
```bash
git clone ssh://git@192.168.1.247:222/lwe/rb-wom-xp-sync.git .
```

If copying files manually:
```bash
# From your Mac
scp -r ~/path/to/rb-wom-xp-sync/* lwe@192.168.1.247:/home/lwe/docker/xp-sync/
```

### 3. Copy Google service account key

From your Mac:
```bash
scp bsbg-sync-b5fb6a0cf715.json lwe@192.168.1.247:/home/lwe/docker/xp-sync/secrets/sa.json
ssh lwe-pc "chmod 600 /home/lwe/docker/xp-sync/secrets/sa.json"
```

### 4. Create .env file

On lwe-pc:
```bash
cd /home/lwe/docker/xp-sync
cp .env.example .env
nano .env
```

Fill in:
```bash
WOM_API_KEY=<your-wom-api-key-or-empty>
WOM_USER_AGENT=bsbg-xp-sync (Discord: YourHandle)
LOG_LEVEL=INFO
INTERVAL_SECONDS=900
DISCORD_WEBHOOK_URL=<optional>
```

### 5. Create config/sheets.txt

```bash
mkdir -p /home/lwe/docker/xp-sync/config
echo "1ndltzPH59D2wQjZg5pIXBnk8HZ5DxBbkIuz_QM9UI20" > /home/lwe/docker/xp-sync/config/sheets.txt
# Add more team IDs on separate lines as needed
```

### 6. Build

```bash
docker compose build
```

### 7. One-time test

```bash
docker compose run --rm xp-sync python sync.py --once
```

**Expected output:**
```
INFO xp-sync: starting sync
INFO xp-sync: sheet 1ndt...: 1 ok, 0 failed
```

**Check the sheet:**
- Open BSBG team sheet → Roster tab
- Row 7 should have gains in columns D:L (0 before EVENT_START_DATE)
- C7 should show "ok"
- B7 should have a timestamp
- B2:C2 should show "1 ok, 0 failed"

If it fails, check:
- Sheet shared with service account email ✓
- Roster tab exists ✓
- Username in A7 ✓
- WOM API key correct (if using one) ✓

### 8. Event start

Nothing to run. The window is set in `docker-compose.yml` (`EVENT_START_DATE` / `EVENT_END_DATE`). Have players log out or hop before the start (see README, "Hiscores lag"). Keep the container up across `EVENT_START_DATE`: it schedules a sync 5 seconds after the start so every player gets a snapshot right at the start, then syncs every 15 min on the clock. Gains come from WOM's `/players/:username/gained` for the event window.

### 9. Start the service

```bash
docker compose up -d
docker compose ps
```

Expected: `xp-sync    running (healthy)` or `(starting)` after ~5 minutes.

Watch the first cycle:
```bash
docker compose logs -f --tail=20
# Ctrl+C to exit
```

You should see one sync every 15 minutes.

## Monitoring

**Check status:**
```bash
docker compose ps
docker compose logs --tail=50
```

**Health check:**
```bash
docker compose exec xp-sync python healthcheck.py && echo "OK" || echo "FAILED"
```

**Manual test (anytime):**
```bash
docker compose run --rm xp-sync python sync.py --once
```

## Adding a team

1. Create a copy of the first team sheet
2. Clear A7:A56 (usernames)
3. Share the new sheet with `xp-sync@bsbg-sync.iam.gserviceaccount.com` as Editor
4. Copy the sheet ID and add to config/sheets.txt:
   ```bash
   echo "<new-sheet-id>" >> /home/lwe/docker/xp-sync/config/sheets.txt
   ```
5. No rebuild needed; service picks it up on next cycle

## Troubleshooting

**Container won't start:**
```bash
docker compose logs
docker compose build --no-cache
```

**Healthcheck fails:**
Means no successful sync in ~37.5 minutes (2.5× default interval).
```bash
docker compose logs | tail -50
```
Common causes: sheet not shared, WOM API down, network issue.

**"403 PERMISSION_DENIED":**
Sheet not shared with service account email.
- Open sheet → Share → Add `xp-sync@bsbg-sync.iam.gserviceaccount.com` as Editor
- Run test: `docker compose run --rm xp-sync python sync.py --once`

**"Requested entity was not found":**
Wrong sheet ID or no Roster tab.
- Verify ID in config/sheets.txt matches sheet URL
- Verify tab is named "Roster" (case-sensitive)

**"rate limited":**
WOM is rate-limiting. Script sleeps and retries. Get a WOM API key if you don't have one (allows ~100 req/min vs ~20 without).

## Stopping and restarting

```bash
# Stop
docker compose down

# Restart (preserves state)
docker compose up -d

# Full rebuild
docker compose down
docker compose build --no-cache
docker compose up -d
```

## Key rotation

**WOM API key exposed:**
- Request new key
- Update WOM_API_KEY in .env
- Restart: `docker compose restart`

**Google service account key exposed:**
- Delete key in Google Cloud Console
- Create new key (JSON)
- Place at secrets/sa.json, chmod 600
- Restart: `docker compose restart`

## Logs and debugging

```bash
docker compose logs --tail=100 | grep -i error
docker compose logs -f   # follow live
docker compose logs --since 10m   # last 10 minutes
```

Set `LOG_LEVEL=DEBUG` in .env for verbose output.

---

See ARCHITECTURE.md for system design and REVIEW.md for code assessment.
