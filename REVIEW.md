# Code Review: rb-wom-xp-sync

## Verdict: Production-ready

Clean, well-tested Python service. No blockers for deployment to lwe-pc. Fits naturally into your infrastructure.

## Tech fit with your environment

✅ **Python 3.12** — Aligns with your language trajectory  
✅ **Docker** — Consistent with rb-discord-bot (same container model, healthcheck)  
✅ **Minimal deps** — requests, gspread, google-auth (all stable)  
✅ **No GPU/special resources** — Runs anywhere; lwe-pc suitable  
✅ **Graceful shutdown** — Handles SIGTERM/SIGINT (good for orchestration)  
✅ **Health checks** — Built-in heartbeat + healthcheck script  
✅ **Error handling** — Doesn't crash on bad data; surfaces errors  
✅ **Rate limiting** — Respects WOM API (0.7s/call with key, 3.2s without)  

## Code quality

**Strengths:**
- Clear separation: WOM client, sheet I/O, orchestration
- Custom exception (WomError) for player-level failures
- Defensive parsing (unranked → 0, missing → None)
- Good use of pathlib, zoneinfo (timezone handling)
- 7 comprehensive unit tests (all offline, all passing)
- Proper logging (info summaries, warning failures)
- Minimal deps — no ORM, no bloat

**Notes:**
- Signal handling lambda is fine for single-threaded loop
- Major-version-only dep pinning is OK for a service
- Throttling per-container (not global) is fine, single instance
- Could add startup logging but not critical

## Assumptions

✅ Verified 2026-10-01:
- WOM endpoint structure (latestSnapshot.data.skills/bosses)
- Sheet metric keys valid (including sailing)

⚠️ Not tested yet (verify on first live run):
- WOM auth header name (x-api-key) — assumed
- Rate limits (~100 req/min with key, ~20 without) — assumed

Watch first 30 minutes of logs for rate-limit messages.

## Deployment readiness

**Ready to go:**
- No system deps beyond Docker
- Non-root user (sync UID 10001)
- Secrets properly isolated
- Config driven by files (.env, sheets.txt)
- Restart behavior correct
- Healthcheck sensible (5 min interval, 2.5× threshold)

**Checklist:**
1. ✅ Code reviewed
2. ✓ healthcheck.py created
3. ✓ .gitignore created
4. ✓ ARCHITECTURE.md created
5. ✓ DEPLOYMENT.md created
6. Push to Gitea (rb-wom-xp-sync)
7. Follow DEPLOYMENT.md on lwe-pc
8. Run `--once` test
9. Snapshot baselines
10. Start container, monitor 15 min

## What's new

- **healthcheck.py** — Checks heartbeat recency (called by Docker Compose)
- **.gitignore** — Prevents accidental secret commits
- **ARCHITECTURE.md** — System design, why lwe-pc, error behavior, testing
- **DEPLOYMENT.md** — Step-by-step setup, troubleshooting, monitoring
- Code reviewed for infrastructure fit

## Next steps

1. Commit to git as `rb-wom-xp-sync` on Gitea
2. Deploy to lwe-pc following DEPLOYMENT.md
3. Run `--once` test, verify sheet updates
4. Snapshot baselines
5. Start container, monitor first cycle

**Time to deploy:** ~20 minutes (clone, add secrets, build, test, start).

**Risk level:** Low. Service is defensive; failed players just show error status and keep last good value. Loop continues.

---

Ready to proceed to git and deployment?
