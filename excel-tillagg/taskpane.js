// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Magnus Kolsjö
// Sidopanelens logik: sök mot /sok, fasettfilter, och infoga i bladet.

const API_BAS = "https://localhost:8443";

let _senasteTraffar = [];

Office.onReady((info) => {
  if (info.host !== Office.HostType.Excel) return;
  document.getElementById("sokKnapp").addEventListener("click", sok);
  document.getElementById("fraga").addEventListener("keydown", (e) => {
    if (e.key === "Enter") sok();
  });
  // Fasetter fungerar som fristående filter — välj och få resultat direkt.
  // Varje ändring räknar också om de övriga listorna, så att inget val som
  // ger noll träffar står kvar och lockar.
  document.getElementById("amneFilter").addEventListener("change", fasettByte);
  document.getElementById("kallaFilter").addEventListener("change", () => {
    paKallaByte();
    fasettByte();
  });
  document.getElementById("skolformFilter").addEventListener("change", fasettByte);
  document.getElementById("huvudmanFilter").addEventListener("change", fasettByte);
  document.getElementById("dimTillbaka").addEventListener("click", stangDimVy);
  document.getElementById("dimInfoga").addEventListener("click", infogaDimData);
  uppdateraFasetter();
});

// ---------------------------------------------------------------------------
// Filterlistor — samfiltrerade
// ---------------------------------------------------------------------------
// Fasetterna begränsar varandra: väljer man ett ämne krymper källistan till
// de myndigheter som har det ämnet, och väljer man en källa krymper
// ämneslistan till det källan har. Alternativ som skulle ge noll träffar
// visas inte alls — annars tvingas man experimentera sig fram till en
// kombination som finns, och tror under tiden att data saknas.
//
// Källorna visas med myndighetens namn; nyckeln ligger i option.value.

async function uppdateraFasetter() {
  const p = new URLSearchParams();
  const kallor = valdaKallor();
  if (kallor.length) p.set("kallor", kallor.join(","));
  const amne = document.getElementById("amneFilter").value;
  if (amne) p.set("amne", amne);
  const skolform = document.getElementById("skolformFilter").value;
  if (skolform) p.set("skolform", skolform);
  const huvudman = document.getElementById("huvudmanFilter").value;
  if (huvudman) p.set("huvudman", huvudman);

  let val;
  try {
    val = await hamtaJson("/sok/fasettval?" + p.toString());
  } catch (e) {
    // Går API:t inte att nå lämnas listorna som de är. En tom panel vore
    // värre än en inaktuell.
    return;
  }
  fyllFasett("kallaFilter", val.kallor || [], null);
  fyllFasett("amneFilter", val.amne || [], "Alla ämnen");
  fyllFasett("skolformFilter", val.skolform || [], "Alla skolformer");
  fyllFasett("huvudmanFilter", val.huvudman || [], "Alla huvudmän");
}

function fyllFasett(id, poster, tomtext) {
  const sel = document.getElementById(id);
  if (!sel) return;
  // Markeringarna ska överleva att listan fylls om — annars nollställs
  // användarens val varje gång en annan fasett ändras.
  const valda = new Set([...sel.selectedOptions].map((o) => o.value).filter(Boolean));
  sel.innerHTML = "";
  if (tomtext !== null) {
    const tom = document.createElement("option");
    tom.value = "";
    tom.textContent = tomtext;
    sel.appendChild(tom);
  }
  for (const x of poster) {
    const opt = document.createElement("option");
    opt.value = x.varde;
    opt.textContent = `${x.namn || x.varde} (${x.antal})`;
    if (valda.has(x.varde)) opt.selected = true;
    sel.appendChild(opt);
  }
}


function valdaKallor() {
  return [...document.getElementById("kallaFilter").selectedOptions]
    .map((o) => o.value)
    .filter(Boolean);
}

async function fasettByte() {
  await uppdateraFasetter();
  sok();
}

function paKallaByte() {
  document
    .querySelector(".skolfilter")
    .classList.toggle("synlig", valdaKallor().includes("skolverket"));
}

// ---------------------------------------------------------------------------
// Sök
// ---------------------------------------------------------------------------

async function sok() {
  const fraga = document.getElementById("fraga").value.trim();
  const amne = document.getElementById("amneFilter").value;
  const kallor = valdaKallor();
  const harFraga = fraga.length >= 2;

  // Inget att göra om varken fråga eller fasett är satt.
  if (!harFraga && !amne && !kallor.length) {
    document.getElementById("traffar").innerHTML = "";
    visaStatus("Skriv minst två tecken, eller välj ämne/källa.");
    return;
  }

  const params = new URLSearchParams({ n: "25" });
  if (harFraga) params.set("q", fraga);
  if (amne) params.set("amne", amne);
  if (kallor.length) params.set("kallor", kallor.join(","));
  if (kallor.includes("skolverket")) {
    const sf = document.getElementById("skolformFilter").value;
    const hm = document.getElementById("huvudmanFilter").value;
    if (sf) params.set("skolform", sf);
    if (hm) params.set("huvudman", hm);
  }

  visaStatus(harFraga ? "Söker…" : "Hämtar…");
  try {
    const traffar = await hamtaJson("/sok?" + params.toString());
    _senasteTraffar = traffar;
    visaTraffar(traffar);
  } catch (e) {
    visaStatus("Misslyckades: " + e.message);
  }
}

function visaTraffar(traffar) {
  const ul = document.getElementById("traffar");
  ul.innerHTML = "";
  if (!traffar.length) {
    visaStatus("Inga träffar.");
    return;
  }
  visaStatus(traffar.length + " träffar.");
  traffar.forEach((t, i) => {
    const li = document.createElement("li");
    li.className = "traff";
    const besk = [t.beskrivning].filter(Boolean).join(" ");
    li.innerHTML = `
      <div class="traff-topp">
        <span class="traff-namn"></span>
        <span class="badge"></span>
      </div>
      <div class="traff-besk"></div>
      <div class="traff-knappar">
        <button class="infoga">Infoga</button>
        <button class="formel">Som formel</button>
      </div>`;
    li.querySelector(".traff-namn").textContent = t.namn || t.id || "(namnlös)";
    li.querySelector(".badge").textContent = `${t.kalla} · ${t.typ}`;
    li.querySelector(".traff-besk").textContent = besk;
    const harMal = t.hamta_api || t.extern_url;
    const infogaKnapp = li.querySelector(".infoga");
    // Dimensionella källor (SCB, Socialstyrelsen) öppnar urvalssteg.
    if (dimensionellKalla(t)) infogaKnapp.textContent = "Välj data";
    infogaKnapp.disabled = !t.hamta_api;
    infogaKnapp.addEventListener("click", () => infoga(t));
    li.querySelector(".formel").addEventListener("click", () => somFormel(t));
    if (!harMal) li.querySelector(".formel").disabled = true;
    ul.appendChild(li);
  });
}

// ---------------------------------------------------------------------------
// Infoga i bladet
// ---------------------------------------------------------------------------

async function infoga(traff) {
  const kind = dimensionellKalla(traff);
  if (kind) return oppnaDimensionsval(traff, kind);
  if (!traff.hamta_api) {
    visaStatus("Den här träffen pekar mot en extern källa — använd länken i stället.");
    return;
  }
  visaStatus("Hämtar och infogar…");
  try {
    const data = await hamtaJson(traff.hamta_api);
    const { rubriker, rader } = tillTabell(data);
    if (!rader.length) {
      visaStatus("Inget data att infoga.");
      return;
    }
    await skrivTillBlad(rubriker, rader, traff.namn);
    visaStatus(`Infogade ${rader.length} rader.`);
  } catch (e) {
    visaStatus("Kunde inte infoga: " + e.message);
  }
}

function somFormel(traff) {
  const mal = traff.hamta_api || traff.extern_url;
  if (!mal) return;
  const formel = `=DOA.HAMTA("${mal.replace(/"/g, '""')}")`;
  Excel.run(async (ctx) => {
    const cell = ctx.workbook.getActiveCell();
    cell.formulas = [[formel]];
    await ctx.sync();
  }).then(() => visaStatus("Formel infogad i markerad cell."));
}

// Normaliserar API-svar till {rubriker, rader} oavsett form.
function tillTabell(data) {
  if (Array.isArray(data)) {
    if (!data.length) return { rubriker: [], rader: [] };
    if (typeof data[0] === "object" && data[0] !== null) {
      const rubriker = [...new Set(data.flatMap((r) => Object.keys(r)))];
      const rader = data.map((r) => rubriker.map((k) => cellvarde(r[k])));
      return { rubriker, rader };
    }
    return { rubriker: ["värde"], rader: data.map((v) => [cellvarde(v)]) };
  }
  if (data && typeof data === "object") {
    // Ett objekt → två-kolumns nyckel/värde-tabell.
    const rader = Object.entries(data).map(([k, v]) => [k, cellvarde(v)]);
    return { rubriker: ["fält", "värde"], rader };
  }
  return { rubriker: ["värde"], rader: [[cellvarde(data)]] };
}

function cellvarde(v) {
  if (v === null || v === undefined) return "";
  if (typeof v === "object") return JSON.stringify(v);
  return v;
}

async function skrivTillBlad(rubriker, rader, titel) {
  await Excel.run(async (ctx) => {
    const blad = ctx.workbook.worksheets.getActiveWorksheet();
    const start = ctx.workbook.getActiveCell();
    start.load("rowIndex, columnIndex");
    await ctx.sync();

    const r0 = start.rowIndex;
    const c0 = start.columnIndex;
    const allt = [rubriker, ...rader];
    const omrade = blad.getRangeByIndexes(r0, c0, allt.length, rubriker.length);
    omrade.values = allt;

    const rubrikrad = blad.getRangeByIndexes(r0, c0, 1, rubriker.length);
    rubrikrad.format.font.bold = true;
    rubrikrad.format.fill.color = "#0b57a4";
    rubrikrad.format.font.color = "white";
    omrade.format.autofitColumns();
    await ctx.sync();
  });
}

// ---------------------------------------------------------------------------
// SCB-dimensionsval
// ---------------------------------------------------------------------------

let _dimTabellId = null;
let _dimMeta = null;
let _dimState = []; // [{ v, mode: "alla"|"totalt"|"urval", selected: Set }]
let _dimKalla = null; // "scb" | "soc"

let _v1Myndighet = null;
let _v1Sokvag = null;

// Vilken sorts dimensionellt urvalssteg en träff behöver (eller null).
function dimensionellKalla(t) {
  const h = t.hamta_api || "";
  if (h.includes("/pxweb-2/metadata")) return "scb";
  if (h.includes("/pxweb-1/metadata")) return "pxweb1";
  if (h.includes("/socialstyrelsen/dimensioner")) return "soc";
  if (h.includes("/trafa/struktur")) return "trafa";
  return null;
}

function oppnaDimensionsval(traff, kind) {
  _dimKalla = kind;
  if (kind === "soc") return oppnaSocDim(traff);
  if (kind === "pxweb1") return oppnaPxweb1Dim(traff);
  if (kind === "trafa") return oppnaTrafaDim(traff);
  return oppnaScbDim(traff);
}

async function oppnaPxweb1Dim(traff) {
  const u = new URL(traff.hamta_api, API_BAS);
  _v1Myndighet = u.searchParams.get("myndighet");
  _v1Sokvag = u.searchParams.get("sokvag");
  visaStatus("Hämtar tabellstruktur…");
  let md;
  try {
    md = await hamtaJson(traff.hamta_api);
  } catch (e) {
    visaStatus("Kunde inte hämta tabellstruktur: " + e.message);
    return;
  }
  // Normalisera v1-metadata till samma form som SCB v2 så renderDim kan återanvändas.
  const variabler = (md.variables || []).map((v) => ({
    id: v.code,
    label: v.text,
    values: v.values || [],
    valueTexts: v.valueTexts || v.value_texts || [],
    time: !!v.time,
    elimination: !!v.elimination,
    truncated: false,
    total_values: (v.values || []).length,
    codelists: [],
  }));
  document.querySelector(".sok").hidden = true;
  document.querySelector(".resultat").hidden = true;
  document.getElementById("dimVy").hidden = false;
  document.getElementById("dimTabell").textContent = md.title || md.label || traff.namn;
  visaStatus("");
  byggDimFalt(variabler);
  uppdateraEstimat();
}

async function oppnaScbDim(traff) {
  const tid = traff.id;
  _dimTabellId = tid;
  visaStatus("Hämtar tabellstruktur…");
  try {
    _dimMeta = await hamtaJson(
      `/pxweb-2/metadata?tabell_id=${encodeURIComponent(tid)}&max_varden_per_dim=200`
    );
  } catch (e) {
    visaStatus("Kunde inte hämta tabellstruktur: " + e.message);
    return;
  }
  document.querySelector(".sok").hidden = true;
  document.querySelector(".resultat").hidden = true;
  document.getElementById("dimVy").hidden = false;
  document.getElementById("dimTabell").textContent = _dimMeta.label || tid;
  visaStatus("");
  byggDimFalt(_dimMeta.variables || []);
  uppdateraEstimat();
}


// ---------------------------------------------------------------------------
// Trafikanalys
// ---------------------------------------------------------------------------
// Trafa har varken PxWeb-metadata eller en dimensionslista — strukturen är ett
// träd där varje post bär en typ:
//
//   P   produkt. Svaret innehåller hela produktkatalogen oavsett vad man
//       frågar efter, så de måste filtreras bort för att produktens egna
//       poster ska synas.
//   D   dimension, med barn av typ DV (värden) eller F (genvägar som
//       "senaste" och "forra").
//   M   mått. Minst ett måste väljas — en fråga med bara produkt och
//       dimensioner ger noll rader för flera produkter.
//   H   hierarkinod för gruppering i Trafas eget gränssnitt. Den går inte
//       att fråga på; API:et felar. Fälten som heter "region" är sådana,
//       vilket är varför Trafa saknar geografisk indelning hos oss.
//
// Frågespråket är `produkt|variabel:värde,värde|mått`.

let _trafaProdukt = null;
let _trafaMatt = [];

async function oppnaTrafaDim(traff) {
  const u = new URL(traff.hamta_api, API_BAS);
  _trafaProdukt = u.searchParams.get("query") || traff.id;
  visaStatus("Hämtar produktstruktur…");
  let s;
  try {
    s = await hamtaJson(`/trafa/struktur?query=${encodeURIComponent(_trafaProdukt)}`);
  } catch (e) {
    visaStatus("Kunde inte hämta produktstruktur: " + e.message);
    return;
  }

  const egna = (s.StructureItems || []).filter((x) => x.Type !== "P");
  const dims = egna.filter((x) => x.Type === "D");
  _trafaMatt = egna.filter((x) => x.Type === "M");

  if (!_trafaMatt.length) {
    visaStatus("Produkten saknar mått och går inte att hämta data ur.");
    return;
  }

  // Normalisera till samma form som PxWeb-variabler, så att byggDimFalt och
  // renderDim kan återanvändas rakt av.
  const variabler = dims.map((d) => {
    const varden = (d.StructureItems || []).filter((b) => b.Type === "DV");
    return {
      id: d.Name,
      label: d.Label || d.Name,
      values: varden.map((b) => String(b.Name)),
      valueTexts: varden.map((b) => b.Label || String(b.Name)),
      time: d.Name === "ar",
      elimination: true,          // Trafa summerar över utelämnade variabler
      truncated: false,
      total_values: varden.length,
      codelists: [],
    };
  });

  // Måtten blir ett eget fält: Trafa kräver minst ett, till skillnad från
  // PxWeb där måttet är en vanlig dimension.
  variabler.unshift({
    id: "__matt",
    label: "Mått (minst ett krävs)",
    values: _trafaMatt.map((m) => m.Name),
    valueTexts: _trafaMatt.map((m) => m.Label || m.Name),
    time: false,
    elimination: false,
    truncated: false,
    total_values: _trafaMatt.length,
    codelists: [],
  });

  document.querySelector(".sok").hidden = true;
  document.querySelector(".resultat").hidden = true;
  document.getElementById("dimVy").hidden = false;
  document.getElementById("dimTabell").textContent = traff.namn || _trafaProdukt;
  visaStatus("");
  byggDimFalt(variabler);
  uppdateraEstimat();
}

function byggTrafaQuery() {
  const led = [];
  const matt = [];
  for (const st of _dimState) {
    const kod = st.v && st.v.id;
    if (!kod) continue;
    const valda = [...st.selected];
    if (kod === "__matt") {
      // Måttet är obligatoriskt och har inget "alla"-läge i frågespråket —
      // varje valt mått blir ett eget led sist i strängen.
      matt.push(...(st.mode === "alla" ? st.v.values : valda));
      continue;
    }
    if (st.mode === "totalt") continue;   // utelämnad = Trafa summerar över den
    if (st.mode === "alla") {
      led.push(kod);                      // bar variabel = alla värden
      continue;
    }
    if (valda.length) led.push(`${kod}:${valda.join(",")}`);
  }
  if (!matt.length) return null;
  return [_trafaProdukt, ...led, ...matt].join("|");
}

async function infogaTrafaData() {
  const query = byggTrafaQuery();
  if (!query) {
    visaStatus("Välj minst ett mått — Trafa ger noll rader utan det.");
    return;
  }
  visaStatus("Hämtar data…");
  document.getElementById("dimInfoga").disabled = true;
  try {
    const svar = await hamtaJson(`/trafa/data?query=${encodeURIComponent(query)}`);
    const rader = svar.datapunkter || svar.Rows || [];
    if (!rader.length) {
      visaStatus("Inga rader för det urvalet.");
      return;
    }
    const rubriker = Object.keys(rader[0]);
    await skrivTillBlad(rubriker, rader, _trafaProdukt);
    visaStatus(`${rader.length} rader infogade.`);
    stangDimVy();
  } catch (e) {
    visaStatus("Kunde inte hämta data: " + e.message);
  } finally {
    document.getElementById("dimInfoga").disabled = false;
  }
}


function stangDimVy() {
  document.getElementById("dimVy").hidden = true;
  document.querySelector(".sok").hidden = false;
  document.querySelector(".resultat").hidden = false;
}

// Smarta standardval: tid=senaste, mått=alla, eliminerbara=Totalt,
// små dimensioner=alla, stora=första värdet (kräver eget val).
function standardLage(v) {
  const koder = v.values || [];
  const harKodlistor = Array.isArray(v.codelists) && v.codelists.length > 0;
  const isContents =
    v.id === "ContentsCode" || (v.label || "").toLowerCase().includes("innehåll");
  // Kodliste-dimensioner (t.ex. Region) får aldrig läge "alla" — då skulle
  // ["*"] hämta den blandade default-listan i stället för vald indelningstyp.
  // De fylls i av laddaKodlista; håll dem i "urval".
  if (harKodlistor)
    return { v, mode: "urval", selected: new Set(), kodlista: true };
  if (v.time)
    return { v, mode: "urval", selected: new Set(koder.length ? [koder[koder.length - 1]] : []), kodlista: false };
  if (isContents) return { v, mode: "alla", selected: new Set(), kodlista: false };
  if (v.elimination) return { v, mode: "totalt", selected: new Set(), kodlista: false };
  if (!v.truncated && (v.total_values || koder.length) <= 40)
    return { v, mode: "alla", selected: new Set(), kodlista: false };
  return { v, mode: "urval", selected: new Set(koder.length ? [koder[0]] : []), kodlista: false };
}

// Laddar en kodlistas (indelningstyps) värden in i en dimensions multiselect.
async function laddaKodlista(st, box, fyllOptioner, codelistId) {
  try {
    const varden = await hamtaJson(
      `/pxweb-2/kodlista?codelist=${encodeURIComponent(codelistId)}`
    );
    fyllOptioner(varden.map((v) => v.kod), varden.map((v) => v.etikett));
    st.selected = new Set();
    if (varden.length && varden.length <= 25) {
      [...box.options].forEach((o) => (o.selected = true));
      st.selected = new Set([...box.options].map((o) => o.value));
    } else if (varden.length) {
      box.options[0].selected = true;
      st.selected = new Set([box.options[0].value]);
    }
    st.mode = "urval";
    uppdateraEstimat();
  } catch (e) {
    /* kodlista-fel — behåll tom */
  }
}

function byggDimFalt(variabler) {
  _dimState = [];
  const wrap = document.getElementById("dimFalt");
  wrap.innerHTML = "";
  variabler.forEach((v) => {
    const st = standardLage(v);
    _dimState.push(st);
    wrap.appendChild(renderDim(v, st));
  });
}

function mkBtn(text, fn) {
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = text;
  b.addEventListener("click", fn);
  return b;
}

function renderDim(v, st) {
  const el = document.createElement("div");
  el.className = "dim-falt";

  const namn = document.createElement("div");
  namn.className = "dim-namn";
  const titel = document.createElement("span");
  titel.textContent = v.label || v.id;
  const meta = document.createElement("span");
  meta.className = "dim-meta";
  meta.textContent = `${v.total_values ?? (v.values || []).length} värden${v.time ? " · tid" : ""}`;
  namn.appendChild(titel);
  namn.appendChild(meta);
  el.appendChild(namn);

  const valomr = document.createElement("div");
  valomr.className = "dim-kontroller";
  valomr.hidden = st.mode === "totalt";

  // Totalt-toggle för eliminerbara dimensioner
  if (v.elimination) {
    const lbl = document.createElement("label");
    lbl.className = "dim-totalt";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = st.mode === "totalt";
    lbl.appendChild(cb);
    lbl.appendChild(document.createTextNode(" Totalt (summera bort dimensionen)"));
    el.appendChild(lbl);
    cb.addEventListener("change", () => {
      st.mode = cb.checked ? "totalt" : st.selected.size ? "urval" : "alla";
      valomr.hidden = cb.checked;
      uppdateraEstimat();
    });
  }

  const box = document.createElement("select");
  box.multiple = true;
  function fyllOptioner(koder, texter) {
    box.innerHTML = "";
    koder.forEach((c, i) => {
      const o = document.createElement("option");
      o.value = c;
      o.textContent = texter[i] || c;
      if (st.selected.has(c)) o.selected = true;
      box.appendChild(o);
    });
  }
  box.addEventListener("change", () => {
    st.selected = new Set([...box.selectedOptions].map((o) => o.value));
    st.mode = st.kodlista || st.selected.size ? "urval" : "alla";
    uppdateraEstimat();
  });

  if (st.kodlista) {
    // Indelningstyp-väljare (Region: Kommuner/Län/NUTS2…). Välj typ först,
    // sedan populeras listan med den typens regioner — som på SCB:s webb.
    const typrad = document.createElement("div");
    typrad.className = "dim-typ";
    const etik = document.createElement("span");
    etik.textContent = "Indelningstyp: ";
    const typSel = document.createElement("select");
    v.codelists.forEach((cl) => {
      const o = document.createElement("option");
      o.value = cl.id;
      o.textContent = cl.label;
      typSel.appendChild(o);
    });
    // Föredra Kommuner, annars första Valueset, annars första.
    const dflt =
      v.codelists.find((c) => /kommun/i.test(c.id) && c.type === "Valueset") ||
      v.codelists.find((c) => c.type === "Valueset") ||
      v.codelists[0];
    typSel.value = dflt.id;
    typrad.appendChild(etik);
    typrad.appendChild(typSel);
    valomr.appendChild(typrad);
    typSel.addEventListener("change", () =>
      laddaKodlista(st, box, fyllOptioner, typSel.value)
    );
    laddaKodlista(st, box, fyllOptioner, dflt.id);
  } else {
    // Visa klartext-etiketter (valueTexts), inte koderna.
    fyllOptioner(v.values || [], v.valueTexts || v.value_texts || []);
    const stor = v.truncated || (v.total_values || 0) > 40;
    if (stor) {
      const sok = document.createElement("input");
      sok.className = "dim-sok";
      sok.placeholder = "Sök värden (t.ex. ortnamn)…";
      let timer;
      sok.addEventListener("input", () => {
        clearTimeout(timer);
        timer = setTimeout(async () => {
          const term = sok.value.trim();
          if (term.length < 2) return;
          try {
            const sida = await hamtaJson(
              `/pxweb-2/dimensionsvarden?tabell_id=${encodeURIComponent(_dimTabellId)}` +
                `&dimension_id=${encodeURIComponent(v.id)}&innehaller=${encodeURIComponent(term)}&sida_storlek=100`
            );
            fyllOptioner(
              (sida.varden || []).map((x) => x.kod),
              (sida.varden || []).map((x) => x.etikett)
            );
          } catch (e) {
            /* sök ej kritisk */
          }
        }, 300);
      });
      valomr.appendChild(sok);
    }
  }
  valomr.appendChild(box);

  const rad = document.createElement("div");
  rad.className = "dim-knapprad";
  rad.appendChild(
    mkBtn("Alla", () => {
      if (st.kodlista) {
        // För indelningstyper måste alla koder anges explicit (inte ["*"],
        // som skulle ge den blandade default-listan).
        [...box.options].forEach((o) => (o.selected = true));
        st.selected = new Set([...box.options].map((o) => o.value));
        st.mode = "urval";
      } else {
        st.mode = "alla";
        st.selected.clear();
        [...box.options].forEach((o) => (o.selected = false));
      }
      uppdateraEstimat();
    })
  );
  rad.appendChild(
    mkBtn("Rensa", () => {
      st.selected.clear();
      [...box.options].forEach((o) => (o.selected = false));
      st.mode = "urval";
      uppdateraEstimat();
    })
  );
  if (v.time) {
    rad.appendChild(mkBtn("Senaste", () => valjSenaste(box, st, 1)));
    rad.appendChild(mkBtn("Senaste 12", () => valjSenaste(box, st, 12)));
  }
  valomr.appendChild(rad);

  el.appendChild(valomr);
  return el;
}

function valjSenaste(box, st, n) {
  const opts = [...box.options];
  opts.forEach((o) => (o.selected = false));
  opts.slice(Math.max(0, opts.length - n)).forEach((o) => (o.selected = true));
  st.selected = new Set([...box.selectedOptions].map((o) => o.value));
  st.mode = "urval";
  uppdateraEstimat();
}

function byggUrval() {
  const urval = {};
  _dimState.forEach((st) => {
    if (st.mode === "totalt") return; // dimensionen utesluts (summeras bort)
    urval[st.v.id] = st.mode === "alla" ? ["*"] : [...st.selected];
  });
  return urval;
}

async function uppdateraEstimat() {
  const cellEl = document.getElementById("dimCeller");
  const btn = document.getElementById("dimInfoga");
  const tom = _dimState.some((st) => st.mode === "urval" && st.selected.size === 0);
  if (tom) {
    cellEl.textContent = "Välj minst ett värde per dimension (eller Alla/Totalt).";
    cellEl.classList.add("over");
    btn.disabled = true;
    return;
  }
  if (_dimKalla === "trafa") {
    // Trafa har ingen estimat-endpoint. Måttet är obligatoriskt, så det
    // kontrolleras här i stället för att frågan skickas och ger noll rader.
    const mattSt = _dimState.find((st) => st.v && st.v.id === "__matt");
    const antalMatt = mattSt
      ? (mattSt.mode === "alla" ? (mattSt.v.values || []).length : mattSt.selected.size)
      : 0;
    if (!antalMatt) {
      cellEl.textContent = "Välj minst ett mått — utan det ger Trafa noll rader.";
      cellEl.classList.add("over");
      btn.disabled = true;
      return;
    }
    // Utan årsdimension svarar Trafa med noll rader utan att säga varför.
    // Uppmätt: t10011|drivm|itrfslut ger 0, t10011|ar|drivm|itrfslut ger 8.
    const tidSt = _dimState.find((st) => st.v && st.v.time);
    if (tidSt && tidSt.mode === "totalt") {
      cellEl.textContent = "År måste vara med — Trafa ger noll rader utan tidsdimension.";
      cellEl.classList.add("over");
      btn.disabled = true;
      return;
    }
    const komb = _dimState.reduce((a, st) => {
      if (st.v && st.v.id === "__matt") return a;
      if (st.mode === "totalt") return a;
      if (st.mode === "alla") return a * Math.max(st.v.total_values || 1, 1);
      return a * Math.max(st.selected.size, 1);
    }, antalMatt);
    cellEl.textContent = `${komb.toLocaleString("sv-SE")} kombinationer`;
    cellEl.classList.toggle("over", komb > 100000);
    btn.disabled = komb > 100000;
    return;
  }
  if (_dimKalla === "pxweb1") {
    // v1 saknar cell-estimat — räkna kombinationer lokalt.
    const komb = _dimState.reduce((a, st) => {
      if (st.mode === "totalt") return a;
      if (st.mode === "alla") return a * Math.max(st.v.total_values || 1, 1);
      return a * Math.max(st.selected.size, 1);
    }, 1);
    cellEl.textContent = `${komb.toLocaleString("sv-SE")} kombinationer`;
    cellEl.classList.toggle("over", komb > 100000);
    btn.disabled = komb > 100000;
    return;
  }
  try {
    const urval = byggUrval();
    const r = await hamtaJson(
      `/pxweb-2/uppskatta-celler?tabell_id=${encodeURIComponent(_dimTabellId)}` +
        `&urval=${encodeURIComponent(JSON.stringify(urval))}`
    );
    cellEl.textContent = `${r.antal.toLocaleString("sv-SE")} celler (tak ${r.tak.toLocaleString("sv-SE")})`;
    cellEl.classList.toggle("over", !r.far_plats);
    btn.disabled = !r.far_plats || r.antal === 0;
  } catch (e) {
    cellEl.textContent = "Kunde inte uppskatta storlek.";
    btn.disabled = false;
  }
}

function infogaDimData() {
  if (_dimKalla === "soc") return infogaSocData();
  if (_dimKalla === "pxweb1") return infogaV1Data();
  if (_dimKalla === "trafa") return infogaTrafaData();
  return infogaScbData();
}

// PxWeb v1 har ett annat query-format än v2:
//   [{code, selection:{filter:"item"|"all", values:[...]}}]
function byggV1Query() {
  const q = [];
  _dimState.forEach((st) => {
    if (st.mode === "totalt") return;
    if (st.mode === "alla")
      q.push({ code: st.v.id, selection: { filter: "all", values: ["*"] } });
    else q.push({ code: st.v.id, selection: { filter: "item", values: [...st.selected] } });
  });
  return q;
}

async function infogaV1Data() {
  const query = byggV1Query();
  visaStatus("Hämtar data…");
  document.getElementById("dimInfoga").disabled = true;
  try {
    const params = new URLSearchParams({
      myndighet: _v1Myndighet,
      sokvag: _v1Sokvag,
      query: JSON.stringify(query),
    });
    const data = await hamtaJson("/pxweb-1/data?" + params.toString());
    const { rubriker, rader } = tillTabell(data);
    if (!rader.length) {
      visaStatus("Inget data för urvalet.");
      return;
    }
    await skrivTillBlad(rubriker, rader, _v1Sokvag);
    visaStatus(`Infogade ${rader.length} rader.`);
    stangDimVy();
  } catch (e) {
    visaStatus("Kunde inte hämta data: " + e.message);
  } finally {
    document.getElementById("dimInfoga").disabled = false;
  }
}

async function infogaScbData() {
  const urval = byggUrval();
  visaStatus("Hämtar SCB-data…");
  document.getElementById("dimInfoga").disabled = true;
  try {
    const data = await hamtaJson(
      `/pxweb-2/data?tabell_id=${encodeURIComponent(_dimTabellId)}` +
        `&urval=${encodeURIComponent(JSON.stringify(urval))}`
    );
    const { rubriker, rader } = tillTabell(data);
    if (!rader.length) {
      visaStatus("Inget data för urvalet.");
      return;
    }
    await skrivTillBlad(rubriker, rader, _dimMeta.label);
    visaStatus(`Infogade ${rader.length} rader från SCB.`);
    stangDimVy();
  } catch (e) {
    visaStatus("Kunde inte hämta data: " + e.message);
  } finally {
    document.getElementById("dimInfoga").disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Socialstyrelsen-dimensionsval (RESTfullt: dimensioner → värden → resultat)
// ---------------------------------------------------------------------------

let _socDb = null;
let _socState = []; // [{ dim, label, values: [{id,text}], selected: Set }]

async function oppnaSocDim(traff) {
  const db = traff.id;
  _socDb = db;
  visaStatus("Hämtar dimensioner…");
  let dims;
  try {
    dims = await hamtaJson(`/socialstyrelsen/dimensioner/${encodeURIComponent(db)}`);
  } catch (e) {
    visaStatus("Kunde inte hämta dimensioner: " + e.message);
    return;
  }
  document.querySelector(".sok").hidden = true;
  document.querySelector(".resultat").hidden = true;
  document.getElementById("dimVy").hidden = false;
  document.getElementById("dimTabell").textContent = traff.namn || db;
  visaStatus("");

  const wrap = document.getElementById("dimFalt");
  wrap.innerHTML = "";
  _socState = [];
  // Hämta värden för varje dimension parallellt.
  await Promise.all(
    dims.map(async (d) => {
      let varden = [];
      try {
        varden = await hamtaJson(
          `/socialstyrelsen/dimensionsvarden/${encodeURIComponent(db)}/${encodeURIComponent(d.namn)}`
        );
      } catch (e) {
        /* hoppa dimension utan värden */
      }
      const st = { dim: d.namn, label: d.text || d.namn, values: varden, selected: new Set() };
      socStandard(st);
      _socState.push(st);
    })
  );
  // Rendera i dimensionernas ordning
  const ordning = dims.map((d) => d.namn);
  _socState.sort((a, b) => ordning.indexOf(a.dim) - ordning.indexOf(b.dim));
  _socState.forEach((st) => wrap.appendChild(renderSocDim(st)));
  uppdateraSocStatus();
}

// Smarta defaults: region=Riket(0), år=senaste, övriga små=alla, stora=första.
function socStandard(st) {
  const ids = st.values.map((v) => String(v.id));
  if (st.dim === "region") {
    const riket = st.values.find((v) => String(v.id) === "0" || /riket/i.test(v.text));
    st.selected = new Set([riket ? String(riket.id) : ids[0]].filter(Boolean));
  } else if (st.dim === "ar") {
    st.selected = new Set(ids.length ? [ids[ids.length - 1]] : []);
  } else if (ids.length <= 25) {
    st.selected = new Set(ids);
  } else {
    st.selected = new Set(ids.length ? [ids[0]] : []);
  }
}

function renderSocDim(st) {
  const el = document.createElement("div");
  el.className = "dim-falt";
  const namn = document.createElement("div");
  namn.className = "dim-namn";
  const titel = document.createElement("span");
  titel.textContent = st.label;
  const meta = document.createElement("span");
  meta.className = "dim-meta";
  meta.textContent = `${st.values.length} värden`;
  namn.appendChild(titel);
  namn.appendChild(meta);
  el.appendChild(namn);

  const kontroller = document.createElement("div");
  kontroller.className = "dim-kontroller";
  const box = document.createElement("select");
  box.multiple = true;
  st.values.forEach((v) => {
    const o = document.createElement("option");
    o.value = String(v.id);
    o.textContent = v.text || v.kod || v.id;
    if (st.selected.has(String(v.id))) o.selected = true;
    box.appendChild(o);
  });
  box.addEventListener("change", () => {
    st.selected = new Set([...box.selectedOptions].map((o) => o.value));
    uppdateraSocStatus();
  });
  kontroller.appendChild(box);

  const rad = document.createElement("div");
  rad.className = "dim-knapprad";
  rad.appendChild(
    mkBtn("Alla", () => {
      [...box.options].forEach((o) => (o.selected = true));
      st.selected = new Set([...box.options].map((o) => o.value));
      uppdateraSocStatus();
    })
  );
  rad.appendChild(
    mkBtn("Rensa", () => {
      [...box.options].forEach((o) => (o.selected = false));
      st.selected = new Set();
      uppdateraSocStatus();
    })
  );
  kontroller.appendChild(rad);
  el.appendChild(kontroller);
  return el;
}

function uppdateraSocStatus() {
  const cellEl = document.getElementById("dimCeller");
  const btn = document.getElementById("dimInfoga");
  const tom = _socState.some((st) => st.selected.size === 0);
  const kombinationer = _socState.reduce((a, st) => a * Math.max(st.selected.size, 1), 1);
  if (tom) {
    cellEl.textContent = "Välj minst ett värde per dimension.";
    cellEl.classList.add("over");
    btn.disabled = true;
  } else {
    cellEl.textContent = `${kombinationer.toLocaleString("sv-SE")} kombinationer`;
    cellEl.classList.toggle("over", kombinationer > 100000);
    btn.disabled = kombinationer > 100000;
  }
}

async function infogaSocData() {
  const params = new URLSearchParams();
  _socState.forEach((st) => params.set(st.dim, [...st.selected].join(",")));
  visaStatus("Hämtar data från Socialstyrelsen…");
  document.getElementById("dimInfoga").disabled = true;
  try {
    const data = await hamtaJson(
      `/socialstyrelsen/resultat/${encodeURIComponent(_socDb)}?${params.toString()}`
    );
    const { rubriker, rader } = tillTabell(data);
    if (!rader.length) {
      visaStatus("Inget data för urvalet.");
      return;
    }
    await skrivTillBlad(rubriker, rader, _socDb);
    visaStatus(`Infogade ${rader.length} rader från Socialstyrelsen.`);
    stangDimVy();
  } catch (e) {
    visaStatus("Kunde inte hämta data: " + e.message);
  } finally {
    document.getElementById("dimInfoga").disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Hjälpare
// ---------------------------------------------------------------------------

async function hamtaJson(sokvag) {
  const url = sokvag.startsWith("http") ? sokvag : API_BAS + sokvag;
  const svar = await fetch(url, { headers: { Accept: "application/json" } });
  if (!svar.ok) throw new Error("HTTP " + svar.status);
  return svar.json();
}

function visaStatus(text) {
  document.getElementById("status").textContent = text;
}
