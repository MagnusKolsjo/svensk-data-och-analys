// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Magnus Kolsjö
// Anpassade Excel-funktioner (DOA.*) mot den lokala FastAPI-tjänsten.
// Alla returnerar en matris (spiller ut över flera celler).

const API_BAS = "https://localhost:8443";

// ---------------------------------------------------------------------------
// Hjälpare
// ---------------------------------------------------------------------------

async function hamtaJson(sokvag) {
  const url = sokvag.startsWith("http") ? sokvag : API_BAS + sokvag;
  const svar = await fetch(url, { headers: { Accept: "application/json" } });
  if (!svar.ok) throw new Error("HTTP " + svar.status + " mot " + sokvag);
  return svar.json();
}

function cellvarde(v) {
  if (v === null || v === undefined) return "";
  if (typeof v === "object") return JSON.stringify(v);
  return v;
}

// Normaliserar ett API-svar till en matris [rubriker, ...rader].
function tillMatris(data) {
  if (Array.isArray(data)) {
    if (!data.length) return [["(inga rader)"]];
    if (typeof data[0] === "object" && data[0] !== null) {
      const rubriker = [...new Set(data.flatMap((r) => Object.keys(r)))];
      return [rubriker, ...data.map((r) => rubriker.map((k) => cellvarde(r[k])))];
    }
    return [["värde"], ...data.map((v) => [cellvarde(v)])];
  }
  if (data && typeof data === "object") {
    return [["fält", "värde"], ...Object.entries(data).map(([k, v]) => [k, cellvarde(v)])];
  }
  return [[cellvarde(data)]];
}

// ---------------------------------------------------------------------------
// Funktioner
// ---------------------------------------------------------------------------

/**
 * Söker i datakällorna och returnerar träffar.
 * @customfunction
 */
async function SOK(fraga, antal) {
  const n = antal && antal > 0 ? Math.floor(antal) : 10;
  const params = new URLSearchParams({ q: fraga, n: String(n) });
  const traffar = await hamtaJson("/sok?" + params.toString());
  const rubriker = ["namn", "källa", "typ", "beskrivning", "hämtväg"];
  const rader = traffar.map((t) => [
    cellvarde(t.namn),
    cellvarde(t.kalla),
    cellvarde(t.typ),
    cellvarde(t.beskrivning),
    cellvarde(t.hamta_api || t.extern_url),
  ]);
  return rader.length ? [rubriker, ...rader] : [["inga träffar"]];
}

/**
 * Hämtar data från en API-sökväg som en tabell.
 * @customfunction
 */
async function HAMTA(sokvag) {
  return tillMatris(await hamtaJson(sokvag));
}

/**
 * Hämtar Kolada-nyckeltal.
 * @customfunction
 */
async function KOLADA(kpi, kommun, ar) {
  const params = new URLSearchParams({ kpi });
  if (kommun) params.set("kommun", kommun);
  if (ar) params.set("ar", ar);
  return tillMatris(await hamtaJson("/kolada/data?" + params.toString()));
}

/**
 * Hämtar en Riksbanksserie mellan två datum.
 * @customfunction
 */
async function RIKSBANK(serie, fran, till) {
  const params = new URLSearchParams();
  if (fran) params.set("fran", fran);
  if (till) params.set("till", till);
  const fraga = params.toString();
  return tillMatris(
    await hamtaJson(`/riksbanken/observationer/${encodeURIComponent(serie)}${fraga ? "?" + fraga : ""}`)
  );
}

// Koppla funktionerna till metadata-id:na.
CustomFunctions.associate("SOK", SOK);
CustomFunctions.associate("HAMTA", HAMTA);
CustomFunctions.associate("KOLADA", KOLADA);
CustomFunctions.associate("RIKSBANK", RIKSBANK);
