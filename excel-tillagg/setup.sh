#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
#
# Sätter upp HTTPS-tjänsten för Office-tillägget på localhost:8443.
# Kör API:t (FastAPI) över TLS så att webbvyn i Excel kan anropa det utan
# mixed-content-block. HTTP :8000 (Power Query) lämnas helt orört.
#
# Stegen:
#   1. Genererar ett självsignerat localhost-cert (om det saknas).
#   2. Installerar och startar en launchd-tjänst för uvicorn med TLS på 8443.
#
# Cert-TRUST i nyckelringen och sideload i Excel görs manuellt — se README.md.

set -euo pipefail

# node --check fångar syntaxfel men inte ett anrop till en funktion som inte
# finns; i Excels webbvy blir det en tyst funktion som aldrig gör något.
python3 "$(dirname "$0")/kontrollera.py" "$(dirname "$0")" || {
  echo "Avbryter: kontrollen hittade fel i tilläggets JavaScript." >&2
  exit 1
}

REPO="$(cd "$(dirname "$0")/.." && pwd)"

# Inställningar som skiljer mellan installationer läses ur repots .env.
if [[ -f "$REPO/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$REPO/.env"
  set +a
fi

VENV="${DOA_VENV:-$REPO/.venv}"
# Ett certifikat kan delas av flera lokala HTTPS-tjänster på samma dator. Det
# identifierar localhost, inte en tjänst — och två självsignerade cert med
# samma CN=localhost går inte att lita på var för sig: nyckelringen ser två
# olika certifikat med samma namn, och ett undantag för den ena porten bär
# inte över till den andra. Kör du fler tjänster, peka DOA_TLS_KATALOG på en
# gemensam katalog.
CERTDIR="${DOA_TLS_KATALOG:-$REPO/certs/localhost}"
ETIKETT="${DOA_LAUNCHD_ETIKETT:-com.magnuskolsjo.doa-api-https}"
PLIST="$HOME/Library/LaunchAgents/$ETIKETT.plist"

echo "== 1. Cert =="
mkdir -p "$CERTDIR"
if [[ -f "$CERTDIR/cert.pem" && -f "$CERTDIR/key.pem" ]]; then
  echo "  Cert finns redan i $CERTDIR — hoppar över."
else
  openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
    -keyout "$CERTDIR/key.pem" -out "$CERTDIR/cert.pem" \
    -subj "/CN=localhost/O=svensk-data-och-analys lokala tjanster" \
    -addext "subjectAltName=DNS:localhost,DNS:*.localhost,IP:127.0.0.1,IP:::1" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,digitalSignature,keyCertSign" \
    -addext "extendedKeyUsage=serverAuth" 2>/dev/null
  chmod 600 "$CERTDIR/key.pem"
  echo "  Genererade self-signed cert i $CERTDIR"
fi

echo "== 2. launchd-tjänst (uvicorn TLS :8443) =="
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$ETIKETT</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VENV/bin/uvicorn</string>
    <string>api.main:app</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>8443</string>
    <string>--ssl-keyfile</string><string>$CERTDIR/key.pem</string>
    <string>--ssl-certfile</string><string>$CERTDIR/cert.pem</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/doa-api-https.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/doa-api-https.log</string>
</dict>
</plist>
PLISTEOF
echo "  Skrev $PLIST"

# Ladda om tjänsten. bootout returnerar innan tjänsten är borta, och en
# bootstrap under tiden felar med "5: Input/output error" — vänta ut den.
launchctl bootout "gui/$(id -u)/$ETIKETT" 2>/dev/null || true
for _ in $(seq 1 20); do
  launchctl print "gui/$(id -u)/$ETIKETT" >/dev/null 2>&1 || break
  sleep 0.5
done
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "  Laddade tjänsten."

echo
echo "Klart. HTTPS-tjänsten lyssnar på https://localhost:8443"
echo "Testa:  curl -k https://localhost:8443/halsa"
echo
echo "Kvar att göra manuellt (se README.md):"
echo "  - Lita på certet i Nyckelhanteraren (annars vägrar Excel ladda tillägget)."
echo "  - Sideloada manifest.xml i Excel."

# ---------------------------------------------------------------------------
# Betroddhet
# ---------------------------------------------------------------------------
# Excels webbvy visar ingen klicka-igenom-dialog. Ett självsignerat cert som
# inte ligger i nyckelringen ger därför inget felmeddelande — funktionerna
# bara misslyckas. Kommandot nedan kräver lösenord och körs av användaren.
if ! security verify-cert -c "$CERTDIR/cert.pem" -p ssl >/dev/null 2>&1; then
  echo
  echo "== Certifikatet är inte betrott än =="
  echo "   Kör en gång (frågar efter ditt lösenord):"
  echo
  echo "   security add-trusted-cert -d -r trustRoot \\"
  echo "     -k /Library/Keychains/System.keychain \\"
  echo "     \"$CERTDIR/cert.pem\""
  echo
  echo "   Starta sedan om webbläsaren och Excel."
fi
