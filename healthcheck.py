#!/usr/bin/env python3
"""Health check for xp-sync container.

Succeeds if the heartbeat file is recent (within 2.5x the sync interval).
"""
import os
import sys
import time
from pathlib import Path

heartbeat = Path(os.environ.get("HEARTBEAT_FILE", "/tmp/xp-sync-heartbeat"))
interval = int(os.environ.get("INTERVAL_SECONDS", "900"))
threshold = interval * 2.5

if not heartbeat.exists():
    print(f"heartbeat not found at {heartbeat}", file=sys.stderr)
    sys.exit(1)

age = time.time() - heartbeat.stat().st_mtime
if age > threshold:
    print(f"heartbeat is {age:.0f}s old (threshold: {threshold:.0f}s)", file=sys.stderr)
    sys.exit(1)

print(f"healthy (last sync {age:.0f}s ago)")
sys.exit(0)
