#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
#
# Bygger installationszippen för QGIS-pluginet.
#
# QGIS installerar plugin ur en zip vars rotmapp måste heta exakt som
# paketet — därav `svensk_data_analys/` i arkivet, inte filerna direkt.
#
# __pycache__ och .DS_Store utesluts. Kompilerade .pyc-filer från en annan
# Python-version får QGIS att ladda gammal kod, vilket är svårt att felsöka
# eftersom källfilen ser rätt ut.
#
# Höj `version=` i metadata.txt före bygget — QGIS erbjuder bara uppdatering
# när versionsnumret ökat.

set -euo pipefail

ROT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAKET="svensk_data_analys"
ZIP="$ROT/$PAKET.zip"

cd "$ROT"

VERSION="$(awk -F= '/^version=/{print $2; exit}' "$PAKET/metadata.txt")"
[ -n "$VERSION" ] || { echo "FEL: hittar ingen version= i metadata.txt"; exit 1; }

# Syntaxkontroll före paketering — en trasig plugin syns annars först när
# QGIS tyst vägrar ladda den.
# compileall fångar syntaxfel men inte ett anrop till en metod som inte
# finns. QGIS laddar pluginet först vid knapptryck, så ett saknat attribut
# syns som en traceback i loggpanelen långt efter att zipen byggts.
python3 "$(dirname "$0")/kontrollera.py" "$PAKET" || {
  echo "Avbryter: kontrollen hittade fel." >&2
  exit 1
}

python3 -m compileall -q "$PAKET" >/dev/null || {
    echo "FEL: $PAKET innehåller syntaxfel"; exit 1; }
find "$PAKET" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

rm -f "$ZIP"
zip -rq "$ZIP" "$PAKET" \
    -x '*/__pycache__/*' '*.pyc' '*/.DS_Store' '.DS_Store'

echo "Byggde $PAKET.zip version $VERSION"
# `head -n -2` finns inte i BSD head; awk gör urvalet portabelt i stället.
unzip -l "$ZIP" | awk '/^ *[0-9]+ /{printf "  %8s  %s\n", $1, $4}'
echo
echo "Installera i QGIS: Insticksprogram → Hantera och installera →"
echo "Installera från ZIP → välj $ZIP"
