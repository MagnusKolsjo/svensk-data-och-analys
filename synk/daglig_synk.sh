#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
#
# Daglig synk — körs av launchd (macOS) eller cron (Linux). Installeras med:
#
#     .venv/bin/python cli/synka.py --installera-schema
#
# Allt som skrivs går till en loggfil per dag i logs/. launchd och cron visar
# inget i någon terminal, så loggen är det enda sättet att se i efterhand vad
# som hände. Loggar äldre än DOA_SYNK_LOGG_DAGAR (standard 30) tas bort.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOGGAR="$REPO/logs"
mkdir -p "$LOGGAR"
exec >> "$LOGGAR/synk-$(date +%Y-%m-%d).log" 2>&1

echo
echo "===== $(date '+%Y-%m-%d %H:%M:%S') — daglig synk startar ====="

if [[ -f "$REPO/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$REPO/.env"
    set +a
fi
PYTHON="${DOA_VENV:-$REPO/.venv}/bin/python"

# Synken avslutar med kod 1 när något steg felat. Loggrensningen ska ändå
# köras, så koden sparas i stället för att avbryta här.
status=0
"$PYTHON" "$REPO/cli/synka.py" || status=$?

find "$LOGGAR" -name "synk-*.log" -mtime +"${DOA_SYNK_LOGG_DAGAR:-30}" -delete

echo "===== $(date '+%Y-%m-%d %H:%M:%S') — daglig synk klar (kod $status) ====="
exit "$status"
