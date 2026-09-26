# Office-tillägg — Svensk data och analys

Sidopanel och anpassade funktioner för Excel som söker i datakällorna
(semantiskt) och infogar data direkt i bladet. Backend är den lokala
FastAPI-tjänsten; tillägget serveras över HTTPS på `localhost:8443` medan
Power Query fortsätter använda HTTP `:8000` oförändrat.

## Innehåll

| Fil | Roll |
|---|---|
| `manifest.xml` | Tilläggsdefinition (sidopanel + funktioner, namespace `DOA`) |
| `taskpane.html/.js/.css` | Sidopanel: sök, fasettfilter, infoga |
| `functions.json/.js/.html` | Anpassade funktioner `DOA.SOK`, `DOA.HAMTA`, `DOA.KOLADA`, `DOA.RIKSBANK` |
| `assets/` | Ikoner |
| `setup.sh` | Genererar cert + startar HTTPS-tjänsten på :8443 |

## Installation

### 1. Starta HTTPS-tjänsten

Från repots rot:

```bash
excel-tillagg/setup.sh
curl -k https://localhost:8443/halsa      # ska ge {"status":"ok"}
```

Skriptet skapar ett självsignerat certifikat för localhost och en
launchd-tjänst som kör API:t över HTTPS. Tre inställningar kan ges i `.env`:

| Variabel | Standard | Används till |
|---|---|---|
| `DOA_TLS_KATALOG` | `certs/localhost` i repot | Katalogen för certifikatet. Peka på en gemensam katalog om flera lokala HTTPS-tjänster ska dela certifikat. |
| `DOA_LAUNCHD_ETIKETT` | `com.magnuskolsjo.doa-api-https` | Namnet på launchd-tjänsten |
| `DOA_VENV` | `.venv` i repot | Pythonmiljön som kör API:t |

### 2. Lita på certet (annars vägrar Excel ladda tillägget)

Self-signed-certet måste litas på i nyckelringen:

```bash
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain \
  certs/localhost/cert.pem
```

Har du satt `DOA_TLS_KATALOG` ligger `cert.pem` där i stället. Skriptet
skriver ut det exakta kommandot om certifikatet inte är betrott.

(Eller öppna `cert.pem` i Nyckelhanteraren → markera *Lita alltid på*.)

### 3. Sideloada i Excel för Mac

Office för Mac läser sideloadade manifest från en wef-mapp:

```bash
mkdir -p ~/Library/Containers/com.microsoft.Excel/Data/Documents/wef
cp excel-tillagg/manifest.xml \
   ~/Library/Containers/com.microsoft.Excel/Data/Documents/wef/
```

Starta om Excel. Tillägget hamnar under **Infoga → Mina tillägg**, och
knappen **Sök data** dyker upp på Start-fliken.

## Användning

**Sidopanel:** klicka *Sök data*, skriv en fråga ("arbetslöshet",
"styrränta", "gymnasium göteborg"), filtrera ev. på källa/skolform/huvudman,
och klicka *Infoga* (skriver data i bladet) eller *Som formel* (infogar
`=DOA.HAMTA(...)`).

**Funktioner** (med parametertips):

| Funktion | Exempel |
|---|---|
| `=DOA.SOK(fråga; [antal])` | `=DOA.SOK("styrränta")` |
| `=DOA.HAMTA(sökväg)` | `=DOA.HAMTA("/kolada/data?kpi=N00945")` |
| `=DOA.KOLADA(kpi; [kommun]; [år])` | `=DOA.KOLADA("N00945";"0180";"2023")` |
| `=DOA.RIKSBANK(serie; [från]; [till])` | `=DOA.RIKSBANK("SECBREPOEFF";"2024-01-01";"2024-12-31")` |

Alla spiller ut sina resultat över flera celler.

## Felsökning

- **Tillägget laddar inte / certfel:** verifiera steg 2 (cert litas på) och
  att `curl -k https://localhost:8443/tillagg/taskpane.html` svarar.
- **Tom panel / inga träffar:** kontrollera att sök-indexet är byggt:
  `curl -k https://localhost:8443/sok/status`.
- **Funktionerna saknas:** ta bort och lägg tillbaka manifestet i wef-mappen
  och starta om Excel; rensa ev. Office-cache.
