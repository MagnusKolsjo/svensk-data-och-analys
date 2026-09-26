# Kända datakvalitetsproblem

Brister i källornas egna data, mätta och dokumenterade här så att de inte
utreds om och inte misstas för fel i suiten.

Listan tar upp fel och luckor — inte egenheter. Att DeSO har koder i stället
för namn, eller att valdistrikt görs om inför varje val, är hur
indelningarna är konstruerade, och den som analyserar på dem vet det. Sådant
tas inte upp här.

Regeln som styr hur de hanteras: **en brist redovisas, den döljs inte och
den gissas inte bort.** Poster som inte går att använda märks i databasen så
att populationen går att fråga fram, och antalet följer med i API-svaren.
Ett tomt fält som tyst hoppas över ser likadant ut som data som saknas —
och en approximation som fyller hålet ser lika trovärdig ut som ett riktigt
värde.

Senast uppdaterad 2026-09-26.

## Skolverket — skolenhetsregistret

### Enheter utan användbar adress

239 av 10 316 enheter (2,3 %) går inte att placera på karta. Märkningen
ligger i `skolenhet_punkt.lage_kalla` och antalen följer med i
`/karta/skolenheter`.

| märkning | antal | vad det är |
|---|---:|---|
| `ej_hittad` | 144 | adressen finns men geokodaren hittar den inte |
| `saknar_gatuadress` | 67 | bara boxadress, trots att gatuadress krävs |
| `endast_ortsniva` | 28 | adressfältet bär kommunnamnet, t.ex. "Grums kommun" |

De två sista är registreringsbrister i ordets egentliga mening: fältet är
ifyllt, men inte med det som efterfrågas.

**Bristen sitter i städerna, inte på landsbygden.** Mätt mot Tillväxtverkets
kommuntyper:

| kommuntyp | oplacerade | andel |
|---|---:|---:|
| Storstadskommuner | 80 av 2 414 | 3,3 % |
| Landsbygdskommuner | 55 av 2 578 | 2,1 % |
| Blandade kommuner | 104 av 5 324 | 2,0 % |

Boxadresserna följer samma mönster — 39 i blandade kommuner och 21 i
storstäder mot 7 på landsbygden. Rimligt: en fristående skola med central
förvaltning anger oftare en box än en kommunal byskola gör.

Den intuitiva förklaringen — glesbygd utan namngivna gator — stämmer alltså
inte, och en fastighetsuppslagning skulle inte lösa särskilt mycket.

### Koordinater som pekar på fel kommun

39 av 7 449 aktiva enheter med koordinat faller inom en annan kommun än
deras egen kommunkod anger. Alla 39 har koordinaten från Skolverket själv;
ingen är geokodad av oss.

Stickprov visar äkta gränsfall: *Sthlm Transport- och fordonstekniska
gymnasiet* uppges Stockholm men ligger i Lindvretens industriområde i
Huddinge, och *Kalvsvik skola* finns med samma namn i både Växjö och
Värmdö.

**Konsekvens för analys:** räknas skolor per kommun via geometrin blir talen
andra än via kommunkoden. Kommunkoden är den auktoritativa uppgiften;
punkten är en adress.

En punkt ligger i Nordsjön — *JENSEN Gymnasium Mölndal* på 59,7°N 2,6°Ö.
Där är källans koordinat fel.

### Täckning

| status | enheter | med koordinat |
|---|---:|---:|
| Aktiv | 7 468 | 99,6 % |
| Vilande | 2 778 | 64 % |
| Planerad | 70 | 64 % |

Bortfallet ligger nästan helt bland vilande enheter, vilket är rimligt —
en nedlagd skola har ingen adress att geokoda.

## Migrationsverket

### Maskerade värden är inte nollor

Två punkter (`..`) i källans filer betyder att talet är litet och dolt av
sekretesskäl. Kommunbladet i *Personer mottagna i en kommun* har **1 624**
sådana celler av knappt 3 500.

Läses de som nollor blir summor för låga och kommuner ser tomma ut.
Klienten returnerar `None` och räknar dem i fältet `maskerade`.

### Ett kommunnamn stavas annorlunda än hos SCB

Uppmätt på kommunbladet i *Personer mottagna i en kommun*: av 289 namn är
**288 exakt lika** SCB:s. Det enda som skiljer är `Upplands-Väsby` med
bindestreck, där SCB skriver `Upplands Väsby` med mellanslag.

`hamta_per_kommun` normaliserar bort skiljetecken före jämförelsen. Det är
billigt och fångar fler varianter om de dyker upp, men problemet är alltså
inte utbrett — det är ett namn.

Bjurholm saknas helt i källans fil. Ingen mottagning där, inte ett fel.

## Socialstyrelsen

Geodatatjänstens områden är **NUTS3** (`RegionCode: SE110`), inte SCB:s
länskoder. En direkt join mot `lan` ger noll träffar; gå via `nuts`.

Databasen `drgstatistikislutenvard` har 306 sjukhus i `region`-dimensionen,
utan kodfält. De är inte områden och går inte att lägga på karta.

### Regiondimensionen betyder olika saker i olika databaser

Det här är viktigare än kodformatet och lätt att missa, eftersom dimensionen
heter `region` överallt. Vad den avser står bara i dimensionens `info`:

| databas | koder | regionen avser |
|---|---|---|
| `amning` | `01`, `03` | hemortslän, där barnet är folkbokfört |
| `lakemedel` | `01`, `03` | patientens folkbokföringsort |
| `graviditeterforlossningarochnyfodda` | `00110`, `00112` | **region där förlossningen skett** |

De databaser som mäter på folkbokföringsort använder SCB:s länskoder. Den
enda som mäter på vårdgivarregion använder en egen femsiffrig numrering —
sannolikt sjukvårdshuvudmannens, men det är inte belagt.

Koderna är inte SCB:s med prefix: `001` + `01` hade gett `00101`, inte
`00110`, och differensen mot SCB vandrar från 109 till 140 genom listan.
Inte heller NUTS3 — bara Stockholm sammanfaller (`SE110`), medan Uppsala är
`SE121` mot Socialstyrelsens `00112`.

Alla 21 länen **matchar på namn** mot `lan`-tabellen, så datat går att
joina. Men en analys som blandar graviditetsdatan med de övriga jämför
förlossningsregion med bosättningslän, och det är inte samma sak — man
föder i grannlänet.

## Val

Gotlands 40 valdistrikt har ingen regionvalkrets — kommunen är också
region, så där hålls inget regionval. Källans tomma cell blev tidigare
strängen `nan` via pandas, vilket gav en 63:e regionvalkrets som inte
finns. Rättat; normaliseras nu till NULL vid inläsning.

## Öppna data-kataloger

Uppmätt 2026-09-26 vid kartläggningen av kommunernas, regionernas och
myndigheternas kataloger (`cli/kartlagg_datadelning.py`).

### Dataportal.se skördar till största delen döda adresser

Av 613 kataloger som dataportal.se skördar från kommuner, regioner och
myndigheter misslyckades 396 vid senaste skördningen. 385 av dem är adresser
av typen `…/datasets/dcat`, konventionen från oppnadata.se-tiden, som i dag
svarar 404. Anmälningarna står kvar, så en organisation ser ut att dela data
trots att den inte gör det.

Registret som sökningen går via tar därför bara med kataloger som svarade och
inte var tomma. De döda anmälningarna redovisas i kartläggningens rapport.

### Lyckad skördning trots att källan inte går att nå

Dataportalen rapporterar senaste skördningen som lyckad för kataloger som inte
går att nå:

- Region Blekinges `oppnadata.regionblekinge.se` är ett CNAME till en
  EntryScape-adress som saknar A-post.
- Rymdstyrelsens katalog i EntryScape Free (kontext 223) svarar
  `401 Not authorized`.

Skördestatus är alltså inget belägg för att katalogen finns. Kartläggningen
prövar själva adressen.

### Metadata som motsäger profilen eller datat

- **SMHI** anger `downloadURL` till webbsidor med formatet `text/html`. Enligt
  DCAT-AP-SE är en `downloadURL` alltid en fil. Hämtningsvägen låter formatet
  avgöra när de motsäger varandra.
- **Jönköping**: Hub-posten "Badplatser" pekar på lager 5 i
  `OpenData/MapServer`, som inte längre finns i tjänsten. Hämtningen svarar
  med de lager som finns.
- **Patent- och registreringsverket**: katalogfilen deklarerar UTF-8 men
  innehåller Windows-1252-tecken. Den läses efter omkodning.
- **Gävle** har katalogposter utan `rdf:type` och utan distributioner. De
  visas med titel och beskrivning men utan något att hämta.
- **Västra Götalandsregionen** länkar från `/psidata` till `data.vgregion.se`,
  som inte finns i DNS.

### Data utan rubrikrad

Eskilstunas badvattentemperaturer är en CSV utan rubrikrad. Kolumnerna får
nummer, och svaret varnar; vad de innehåller står bara i datamängdens
beskrivning.
