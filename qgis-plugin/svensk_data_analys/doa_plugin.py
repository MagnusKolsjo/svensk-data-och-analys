# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""QGIS-plugin: sök svensk statistik och visa på karta.

Pratar med den lokala FastAPI-tjänsten (localhost:8000) — söker via /sok,
hämtar geometri via /karta/geometri och joinar värden via /karta/join. QGIS är
ingen webbläsare-sandbox, så ren HTTP mot localhost räcker (inget cert).
"""

from __future__ import annotations

import csv
import json
import os
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path as _Path

from qgis.core import (
    QgsClassificationQuantile,
    QgsCoordinateReferenceSystem,
    QgsGraduatedSymbolRenderer,
    QgsProject,
    QgsStyle,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

# QAction flyttade från QtWidgets (Qt5) till QtGui (Qt6/QGIS 4).
try:
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5 / QGIS 3
    from qgis.PyQt.QtWidgets import QAction

API = "http://localhost:8000"
# kommun/lan är landklippta (GISCO LAU — strandlinje, korrekt kartbild);
# *_medvatten är de administrativa ytorna inklusive vattenområden.
_OMRADEN = ["kommun", "lan", "deso", "regso", "valdistrikt",
            "kommun_medvatten", "lan_medvatten"]


# ---------------------------------------------------------------------------
# HTTP-hjälpare (urllib — alltid tillgängligt i QGIS Python)
# ---------------------------------------------------------------------------


def _get_json(sokvag: str):
    with urllib.request.urlopen(API + sokvag, timeout=90) as r:
        return json.loads(r.read().decode("utf-8"))


def _get_text(sokvag: str) -> str:
    with urllib.request.urlopen(API + sokvag, timeout=180) as r:
        return r.read().decode("utf-8")


def _post_text(sokvag: str, kropp) -> str:
    data = json.dumps(kropp).encode("utf-8")
    req = urllib.request.Request(
        API + sokvag, data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.read().decode("utf-8")


# ---------------------------------------------------------------------------
# Dimensionsdialog (SCB / PxWeb v2)
# ---------------------------------------------------------------------------


def _kodlista_till_omrade(cl):
    """Mappar en SCB-kodlista till vår områdestyp (None om ej kartbar).

    Bara raka indelningar (Valueset) med matchande geometri stöds —
    aggregeringar (NUTS, kommungrupper, A-regioner) saknar egen geometri här.
    """
    if cl.get("type") != "Valueset":
        return None
    s = (str(cl.get("id", "")) + " " + str(cl.get("label", ""))).lower()
    if "kommun" in s:
        return "kommun"
    if "län" in s or "lan" in s:
        return "lan"
    if "deso" in s:
        return "deso"
    if "regso" in s:
        return "regso"
    return None


class DimDialog(QDialog):
    """Väljer kart-indelningstyp + låser övriga dimensioner till ett värde var.

    För en choropleth blir regiondimensionen kart-områdena (alla kommuner/län
    osv) medan varje annan dimension måste ha exakt ett värde, så att varje
    område får ett tal.
    """

    def __init__(self, parent, metadata):
        super().__init__(parent)
        self.setWindowTitle("Välj dimensioner för kartan")
        self.regdim = next(
            (v for v in metadata.get("variables", []) if v.get("codelists")), None
        )
        form = QFormLayout(self)

        self.typ = QComboBox()
        if self.regdim:
            for cl in self.regdim["codelists"]:
                omr = _kodlista_till_omrade(cl)
                if omr:
                    self.typ.addItem(cl.get("label", omr), (cl["id"], omr))
        form.addRow("Karta över:", self.typ)

        self.dim_combos = {}
        for v in metadata.get("variables", []):
            if self.regdim and v["id"] == self.regdim["id"]:
                continue
            cb = QComboBox()
            koder = v.get("values", [])
            texter = v.get("valueTexts") or koder
            for i, kod in enumerate(koder):
                cb.addItem(texter[i] if i < len(texter) else kod, kod)
            if v.get("time") and cb.count():
                cb.setCurrentIndex(cb.count() - 1)  # senaste år som default
            self.dim_combos[v["id"]] = cb
            form.addRow((v.get("label") or v["id"]) + ":", cb)

        knappar = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        knappar.accepted.connect(self.accept)
        knappar.rejected.connect(self.reject)
        form.addRow(knappar)

    def val(self):
        cl_id, omrade = self.typ.currentData()
        lasta = {dim: cb.currentData() for dim, cb in self.dim_combos.items()}
        return cl_id, omrade, lasta, self.regdim["id"]


# ---------------------------------------------------------------------------
# Dockpanel
# ---------------------------------------------------------------------------


class DoaDock(QDockWidget):
    def __init__(self, iface):
        super().__init__("Svensk data och analys")
        self.iface = iface
        self.setObjectName("DoaDock")

        innehall = QWidget()
        layout = QVBoxLayout()
        innehall.setLayout(layout)

        # Sökrad
        sokrad = QHBoxLayout()
        self.sokfalt = QLineEdit()
        self.sokfalt.setPlaceholderText("Sök, t.ex. arbetslöshet, ohälsotal…")
        self.sokfalt.returnPressed.connect(self.sok)
        sokknapp = QPushButton("Sök")
        sokknapp.clicked.connect(self.sok)
        sokrad.addWidget(self.sokfalt)
        sokrad.addWidget(sokknapp)
        layout.addLayout(sokrad)

        # Fasetter/filter — fungerar fristående (välj utan att skriva i sökrutan)
        amnerad = QHBoxLayout()
        amnerad.addWidget(QLabel("Ämne:"))
        self.amne = QComboBox()
        self.amne.addItem("Alla ämnen", None)
        # Kopplas i slutet av __init__, efter att alla widgetar finns —
        # _amne_byte rör skolform och huvudman som skapas längre ned.
        amnerad.addWidget(self.amne)
        layout.addLayout(amnerad)

        kallrad = QHBoxLayout()
        kallrad.addWidget(QLabel("Källor (Cmd för flera):"))
        kallrad.addStretch()
        # En markerad lista går bara att tömma med Cmd-klick, vilket inte är
        # upptäckbart. Utan en väg tillbaka blir varje val en återvändsgränd:
        # ämneslistan krymper till den valda källans ämnen och man kommer
        # inte ur det.
        rensa = QPushButton("Rensa filter")
        rensa.setToolTip("Nollställer ämne, källor, skolform och huvudman.")
        rensa.clicked.connect(self.rensa_filter)
        kallrad.addWidget(rensa)
        layout.addLayout(kallrad)

        self.kallor = QListWidget()
        self.kallor.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.kallor.setMaximumHeight(90)
        self.kallor.itemSelectionChanged.connect(self._kalla_byte)
        layout.addWidget(self.kallor)

        self.skolrad = QWidget()
        srl = QHBoxLayout()
        self.skolrad.setLayout(srl)
        srl.addWidget(QLabel("Skolform:"))
        self.skolform = QComboBox()
        self.skolform.addItem("Alla", None)
        srl.addWidget(self.skolform)
        srl.addWidget(QLabel("Huvudman:"))
        self.huvudman = QComboBox()
        self.huvudman.addItem("Alla", None)
        srl.addWidget(self.huvudman)
        self.skolrad.setVisible(False)
        layout.addWidget(self.skolrad)

        # Geo-filtret. Förvalt på: den som har pluginet öppet ska göra kartor,
        # och 6 100 av indexets poster går inte att lägga på karta alls.
        self.baragoe = QCheckBox("Bara data som går att lägga på karta")
        self.baragoe.setChecked(True)
        self.baragoe.setToolTip(
            "Filtrerar bort dataserier utan geografisk indelning. Utan det "
            "här syns även räntor, valutakurser och rikstotaler, som inte "
            "går att rita."
        )
        self.baragoe.stateChanged.connect(lambda _: self._filter_byte())
        layout.addWidget(self.baragoe)

        # Träfflista
        self.lista = QListWidget()
        self.lista.currentItemChanged.connect(self._traff_byte)
        layout.addWidget(self.lista)

        # Områdestyp + år
        rad2 = QHBoxLayout()
        rad2.addWidget(QLabel("Område:"))
        self.omrade = QComboBox()
        self.omrade.addItems(_OMRADEN)
        rad2.addWidget(self.omrade)
        rad2.addWidget(QLabel("År:"))
        self.ar = QSpinBox()
        self.ar.setRange(1900, 2100)
        self.ar.setValue(2023)
        rad2.addWidget(self.ar)
        layout.addLayout(rad2)

        # Knappar
        self.visaknapp = QPushButton("Visa vald statistik på karta")
        self.visaknapp.clicked.connect(self.visa_pa_karta)
        layout.addWidget(self.visaknapp)

        baslager = QPushButton("Lägg till områdeslager (bara geometri)")
        baslager.clicked.connect(self.lagg_till_omradeslager)
        layout.addWidget(baslager)

        skolknapp = QPushButton("Lägg till skolenheter som punktlager")
        skolknapp.setToolTip(
            "Skolenheter är punkter, inte ytor — de läggs som eget lager och "
            "kan inte färgläggas som en choropleth. Avgränsas av Område-valet "
            "när det är en kommun eller ett län."
        )
        skolknapp.clicked.connect(self.lagg_till_skolenheter)
        layout.addWidget(skolknapp)

        importknapp = QPushButton("Importera analysresultat (CSV: kod, värde)")
        importknapp.clicked.connect(self.importera_analys)
        layout.addWidget(importknapp)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.setWidget(innehall)

        self.amne.currentIndexChanged.connect(self._amne_byte)
        self.skolform.currentIndexChanged.connect(self._filter_byte)
        self.huvudman.currentIndexChanged.connect(self._filter_byte)
        self._uppdatera_fasetter()

    # -- Fasett-listor -----------------------------------------------------
    # Fasetterna samfiltreras: väljer man ett ämne krymper källistan till de
    # myndigheter som har det ämnet, och väljer man en källa krymper
    # ämneslistan till det källan har. Alternativ som skulle ge noll träffar
    # visas inte alls — annars tvingas man experimentera sig fram till en
    # kombination som finns, och tror under tiden att data saknas.

    def _uppdatera_fasetter(self):
        """Fyller om alla fasettlistor med det som fortfarande är valbart."""
        params = {}
        kallor = self._valda_kallor()
        if kallor:
            params["kallor"] = ",".join(kallor)
        if self.amne.currentData():
            params["amne"] = self.amne.currentData()
        if self.skolform.currentData():
            params["skolform"] = self.skolform.currentData()
        if self.huvudman.currentData():
            params["huvudman"] = self.huvudman.currentData()
        if self.baragoe.isChecked():
            params["geografisk"] = "true"
        try:
            val = _get_json("/sok/fasettval?" + urllib.parse.urlencode(params))
        except Exception as e:  # noqa: BLE001
            # Tidigare returnerade den här grenen tyst, vilket gjorde ett
            # trasigt filter omöjligt att skilja från ett filter som inte
            # gör något. Listorna lämnas orörda men felet syns.
            self._status("Kunde inte uppdatera filtren: %s. Körs doa-api "
                         "på :8000?" % e)
            return

        self._fyll_lista(self.kallor, val.get("kallor", []))
        self._fyll_meny(self.amne, val.get("amne", []), "Alla ämnen")
        self._fyll_meny(self.skolform, val.get("skolform", []), "Alla skolformer")
        self._fyll_meny(self.huvudman, val.get("huvudman", []), "Alla huvudmän")
        self._visa_filterlage(params, val)

    def _visa_filterlage(self, params, val):
        """Skriver ut vad filtret gör, så att effekten går att se.

        Utan det är det omöjligt att skilja "filtret tillämpades och gav
        åtta källor" från "filtret tillämpades aldrig".
        """
        aktiva = [
            "%s=%s" % (k, v) for k, v in sorted(params.items())
            if k != "geografisk"
        ]
        self._status(
            "%d myndigheter, %d ämnen%s" % (
                len(val.get("kallor", [])), len(val.get("amne", [])),
                (" — filter: " + ", ".join(aktiva)) if aktiva else " (inget filter)",
            )
        )

    @staticmethod
    def _fyll_meny(meny, poster, tomtext):
        """Fyller en rullgardin och behåller valet om det finns kvar."""
        tidigare = meny.currentData()
        meny.blockSignals(True)
        meny.clear()
        meny.addItem(tomtext, None)
        for x in poster:
            meny.addItem("%s (%d)" % (x.get("namn") or x["varde"], x["antal"]),
                         x["varde"])
        if tidigare:
            i = meny.findData(tidigare)
            meny.setCurrentIndex(i if i >= 0 else 0)
        meny.blockSignals(False)

    @staticmethod
    def _fyll_lista(lista, poster):
        """Fyller källistan med myndighetsnamn och behåller markeringarna.

        Nyckeln läggs i UserRole; texten är myndighetens namn. Sökningen
        skickar nyckeln, användaren ser namnet.
        """
        valda = {i.data(Qt.ItemDataRole.UserRole) for i in lista.selectedItems()}
        lista.blockSignals(True)
        lista.clear()
        for x in poster:
            item = QListWidgetItem("%s (%d)" % (x["namn"], x["antal"]))
            item.setData(Qt.ItemDataRole.UserRole, x["varde"])
            lista.addItem(item)
            if x["varde"] in valda:
                item.setSelected(True)
        lista.blockSignals(False)

    def _valda_kallor(self):
        """Källnycklarna för det som är markerat.

        Texten i listan är myndighetens namn; nyckeln ligger i UserRole.
        Sökningen ska ha nyckeln, användaren ser namnet.
        """
        return [
            i.data(Qt.ItemDataRole.UserRole) for i in self.kallor.selectedItems()
            if i.data(Qt.ItemDataRole.UserRole)
        ]

    def _kalla_byte(self):
        self.skolrad.setVisible("skolverket" in self._valda_kallor())
        self._uppdatera_fasetter()
        self.sok()

    def rensa_filter(self):
        """Nollställer alla fasettval och fyller listorna på nytt."""
        for meny in (self.amne, self.skolform, self.huvudman):
            meny.blockSignals(True)
            meny.setCurrentIndex(0)
            meny.blockSignals(False)
        self.kallor.blockSignals(True)
        self.kallor.clearSelection()
        self.kallor.blockSignals(False)
        self.skolrad.setVisible(False)
        self._uppdatera_fasetter()
        self.sok()

    def _amne_byte(self, _index=None):
        self._uppdatera_fasetter()
        self.sok()

    def _filter_byte(self):
        self._uppdatera_fasetter()
        self.sok()

    # -- Sök ---------------------------------------------------------------

    def sok(self):
        q = self.sokfalt.text().strip()
        amne = self.amne.currentData() or ""
        kallor = self._valda_kallor()
        har_fraga = len(q) >= 2
        if not har_fraga and not amne and not kallor:
            self.lista.clear()
            self._status("Skriv minst två tecken, eller välj ämne/källa.")
            return

        params = {"n": "40"}
        if har_fraga:
            params["q"] = q
        if amne:
            params["amne"] = amne
        if kallor:
            params["kallor"] = ",".join(kallor)
        if self.baragoe.isChecked():
            params["geografisk"] = "true"
        if "skolverket" in kallor:
            if self.skolform.currentData():
                params["skolform"] = self.skolform.currentData()
            if self.huvudman.currentData():
                params["huvudman"] = self.huvudman.currentData()

        self._status("Söker…" if har_fraga else "Hämtar…")
        try:
            traffar = _get_json("/sok?" + urllib.parse.urlencode(params))
        except Exception as e:  # noqa: BLE001
            self._status("Sökningen misslyckades: %s. Körs doa-api på :8000?" % e)
            return
        self.lista.clear()
        for t in traffar:
            geo = (t.get("fasetter") or {}).get("geo") or {}
            marke = ""
            if geo:
                nivaer = ", ".join(geo.get("nivaer") or [])
                span = ""
                if geo.get("fran_ar") and geo.get("till_ar"):
                    span = " %s-%s" % (geo["fran_ar"], geo["till_ar"])
                marke = "  [%s%s]" % (nivaer, span)
            text = "%s  ·  %s/%s%s" % (
                t.get("namn"), t.get("kalla"), t.get("typ"), marke)
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, t)
            self.lista.addItem(item)
        self._status("%d träffar." % len(traffar))

    def _traff_byte(self, item, _forra=None):
        """Begränsar Område och År till vad den valda träffen faktiskt har.

        Utan det här är Område en hårdkodad lista över kartlager och År en fri
        räknare 1900-2100, oavsett vad datat innehåller. Man fick välja, klicka
        visa, och först då veta om kombinationen fanns.
        """
        geo = {}
        if item:
            traff = item.data(Qt.ItemDataRole.UserRole) or {}
            geo = (traff.get("fasetter") or {}).get("geo") or {}

        nivaer = [n for n in (geo.get("nivaer") or []) if n in _OMRADEN]
        tidigare = self.omrade.currentText()
        self.omrade.blockSignals(True)
        self.omrade.clear()
        # Utan träff visas alla lager — man ska kunna lägga till ett tomt
        # områdeslager utan att först ha sökt.
        self.omrade.addItems(nivaer or _OMRADEN)
        if tidigare in (nivaer or _OMRADEN):
            self.omrade.setCurrentText(tidigare)
        self.omrade.blockSignals(False)

        fran, till = geo.get("fran_ar"), geo.get("till_ar")
        if fran and till:
            self.ar.setRange(int(fran), int(till))
            if not (int(fran) <= self.ar.value() <= int(till)):
                self.ar.setValue(int(till))
            self.ar.setToolTip("Serien täcker %s-%s." % (fran, till))
        else:
            # Kolada uppger inga år i metadatan; att låsa året till en gissning
            # vore värre än att lämna det fritt.
            self.ar.setRange(1900, 2100)
            self.ar.setToolTip("Källan uppger inget årsintervall.")

    # -- Visa statistik på karta ------------------------------------------

    def visa_pa_karta(self):
        item = self.lista.currentItem()
        if not item:
            self._status("Markera en träff i listan först.")
            return
        t = item.data(Qt.ItemDataRole.UserRole)
        hamta = t.get("hamta_api") or ""
        if t.get("kalla") == "kolada" and t.get("typ") == "kpi":
            self._kolada_choropleth(t)
        elif "/pxweb-2/metadata" in hamta:
            self._scb_choropleth(t)  # dimensionsdialog
        else:
            self._status(
                "Den här källan kartläggs inte direkt än (region-koderna matchar "
                "inte geometrin). Välj i Excel-panelen, exportera och använd "
                "'Importera analysresultat'."
            )

    def _scb_choropleth(self, t):
        tid = t.get("id")
        self._status("Hämtar tabellstruktur…")
        try:
            md = _get_json(
                "/pxweb-2/metadata?tabell_id=%s&max_varden_per_dim=2000" % tid
            )
        except Exception as e:  # noqa: BLE001
            self._status("Kunde inte hämta metadata: %s" % e)
            return
        dlg = DimDialog(self, md)
        if dlg.typ.count() == 0:
            self._status(
                "Tabellen saknar en kartbar indelningstyp (kommun/län/DeSO/RegSO)."
            )
            return
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        cl_id, omrade, lasta, regdim_id = dlg.val()
        self._status("Hämtar kart-data (alla områden)…")
        try:
            koder = [
                v["kod"]
                for v in _get_json(
                    "/pxweb-2/kodlista?codelist=" + urllib.parse.quote(cl_id)
                )
            ]
            urval = {regdim_id: koder}
            for dim, kod in lasta.items():
                urval[dim] = [kod]
            data = _get_json(
                "/pxweb-2/data?tabell_id=%s&urval=%s"
                % (tid, urllib.parse.quote(json.dumps(urval)))
            )
        except Exception as e:  # noqa: BLE001
            self._status("Kunde inte hämta data: %s" % e)
            return
        rk = regdim_id + "_kod"
        rader = [
            {"kod": r.get(rk), "varde": r.get("varde")} for r in data if r.get(rk)
        ]
        if not rader:
            self._status("Inget data för urvalet.")
            return
        self._joina_och_lagg_upp(rader, omrade, t.get("namn"))

    def _kolada_choropleth(self, t):
        kpi = t.get("id")
        ar = self.ar.value()
        self._status("Hämtar Kolada %s för %d…" % (kpi, ar))
        try:
            data = _get_json("/kolada/data?kpi=%s&ar=%d&kon=T" % (kpi, ar))
        except Exception as e:  # noqa: BLE001
            self._status("Kunde inte hämta data: %s" % e)
            return
        rader = [
            {"kod": str(r["kommun"]), "varde": r.get("varde")}
            for r in data
            if r.get("kommun") and len(str(r["kommun"])) == 4
        ]
        if not rader:
            self._status("Ingen kommun-data för %d (prova annat år)." % ar)
            return
        self._joina_och_lagg_upp(rader, "kommun", "%s %d" % (t.get("namn"), ar))


    def lagg_till_omradeslager(self):
        typ = self.omrade.currentText()
        self._status("Hämtar %s-geometri…" % typ)
        try:
            gj = _get_text("/karta/geometri?typ=" + typ)
        except Exception as e:  # noqa: BLE001
            self._status("Kunde inte hämta geometri: %s" % e)
            return
        self._ladda_lager(gj, typ, choropleth=False)
        self._status("Lade till områdeslager: %s." % typ)

    def lagg_till_skolenheter(self):
        """Hämtar skolenheter som punktlager.

        Läser suitens spegeltabell, inte Skolverket direkt — koordinaterna
        finns bara i deras detalj-API, ett anrop per enhet, och 6 700 anrop
        hör inte hemma i ett knapptryck. Är tabellen tom säger felet det.
        """
        params = {"status": "Aktiv"}
        # Ett valt kommun- eller länslager avgränsar även punkterna, så att
        # de två lagren visar samma område.
        vald = (self.omrade.currentText() or "").lower()
        if vald.startswith("kommun") and self._valt_omradesfilter():
            params["kommun_kod"] = self._valt_omradesfilter()
        elif vald.startswith("lan") and self._valt_omradesfilter():
            params["lan_kod"] = self._valt_omradesfilter()

        self._status("Hämtar skolenheter…")
        try:
            gj = _get_text("/karta/skolenheter?" + urllib.parse.urlencode(params))
        except Exception as e:  # noqa: BLE001
            self._status("Kunde inte hämta skolenheter: %s" % e)
            return
        try:
            antal = len(json.loads(gj).get("features") or [])
        except Exception:  # noqa: BLE001
            antal = 0
        if not antal:
            self._status(
                "Inga skolenheter med koordinater. Kör "
                "cli/synka_skolenheter.py för att fylla punktlagret."
            )
            return
        self._ladda_lager(gj, "Skolenheter", choropleth=False)
        self._status("Lade till %d skolenheter som punktlager." % antal)

    def _valt_omradesfilter(self):
        """Kommun- eller länskod ur en markerad träff, om det finns en."""
        item = self.lista.currentItem()
        if not item:
            return None
        traff = item.data(Qt.ItemDataRole.UserRole) or {}
        return (traff.get("fasetter") or {}).get("kommunkod")

    # -- Importera analysresultat -----------------------------------------

    def importera_analys(self):
        fil, _ = QFileDialog.getOpenFileName(
            self, "Välj CSV med kod och värde", "", "CSV (*.csv);;Alla filer (*)"
        )
        if not fil:
            return
        try:
            rader = self._las_csv(fil)
        except Exception as e:  # noqa: BLE001
            self._status("Kunde inte läsa CSV: %s" % e)
            return
        if not rader:
            self._status("Hittade inga kod/värde-rader i filen.")
            return
        typ = self.omrade.currentText()
        self._joina_och_lagg_upp(rader, typ, os.path.basename(fil))

    def _las_csv(self, fil):
        # Hitta kod- och värde-kolumn; annars använd de två första kolumnerna.
        with open(fil, newline="", encoding="utf-8-sig") as f:
            prov = f.read(2048)
            f.seek(0)
            try:
                avgr = csv.Sniffer().sniff(prov, delimiters=";,\t").delimiter
            except Exception:  # noqa: BLE001
                avgr = ";" if ";" in prov else ","
            laser = csv.DictReader(f, delimiter=avgr)
            falt = laser.fieldnames or []
            kod_f = self._hitta_falt(falt, ("kod", "kommun", "kommunkod", "region", "länskod", "lan_kod"))
            varde_f = self._hitta_falt(falt, ("varde", "värde", "value", "tal", "andel"))
            if not kod_f and len(falt) >= 1:
                kod_f = falt[0]
            if not varde_f and len(falt) >= 2:
                varde_f = falt[1]
            rader = []
            for rad in laser:
                kod = (rad.get(kod_f) or "").strip()
                if not kod:
                    continue
                rastext = (rad.get(varde_f) or "").strip().replace(",", ".")
                try:
                    varde = float(rastext) if rastext else None
                except ValueError:
                    varde = None
                rader.append({"kod": kod, "varde": varde})
            return rader

    @staticmethod
    def _hitta_falt(falt, kandidater):
        laga = {f.lower(): f for f in falt}
        for k in kandidater:
            if k in laga:
                return laga[k]
        return None

    # -- Gemensamt: join + lägg upp lager ---------------------------------

    def _joina_och_lagg_upp(self, rader, typ, namn):
        self._status("Joinar mot %s-geometri…" % typ)
        try:
            gj = _post_text("/karta/join?typ=" + typ, rader)
        except Exception as e:  # noqa: BLE001
            self._status("Join misslyckades: %s" % e)
            return
        self._ladda_lager(gj, namn, choropleth=True)
        self._status("Lade upp '%s' på karta (%d områden)." % (namn, len(rader)))

    def _ladda_lager(self, geojson_text, namn, choropleth):
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".geojson", delete=False, encoding="utf-8"
        )
        tmp.write(geojson_text)
        tmp.close()
        lager = QgsVectorLayer(tmp.name, namn, "ogr")
        if not lager.isValid():
            self._status("Lagret kunde inte läsas (ogiltig GeoJSON).")
            return
        if choropleth:
            self._stil_choropleth(lager)
        forsta_lagret = not QgsProject.instance().mapLayers()
        QgsProject.instance().addMapLayer(lager)
        if forsta_lagret:
            self._satt_projektion()

    def _satt_projektion(self):
        """Sätter projektets projektion till SWEREF99 TM.

        Geometrin levereras i EPSG:4326, vilket är rätt för lagring och
        GeoJSON-utbyte men fel att rita i. Oprojicerat behandlas en
        longitudgrad som lika bred som en latitudgrad, och eftersom
        longitudgraden krymper mot polerna sträcks kartan i sidled — allt
        mer ju längre norrut. Sverige blir då för brett i Norrbotten och
        för smalt i Skåne.

        EPSG:3006 är den nationella standarden och ger rätt proportioner.
        Sätts bara när projektet är tomt, så att ett medvetet val av
        projektion aldrig skrivs över.
        """
        try:
            QgsProject.instance().setCrs(
                QgsCoordinateReferenceSystem("EPSG:3006")
            )
            self._status("Projektionen satt till SWEREF99 TM (EPSG:3006).")
        except Exception:  # noqa: BLE001
            # Projektionen är kosmetik — den får aldrig fälla lagerinläsningen.
            pass

    def _stil_choropleth(self, lager):
        # Modernt API (QGIS 3.10+/4.x): klassificeringsmetod + updateClasses.
        # Det gamla createRenderer + Mode.Quantile-enum är borttaget i QGIS 4.
        try:
            renderer = QgsGraduatedSymbolRenderer("varde")
            renderer.setClassificationMethod(QgsClassificationQuantile())
            ramp = (
                QgsStyle.defaultStyle().colorRamp("Viridis")
                or QgsStyle.defaultStyle().colorRamp("Blues")
            )
            if ramp is not None:
                renderer.updateColorRamp(ramp)
            renderer.updateClasses(lager, 5)
            lager.setRenderer(renderer)
            lager.triggerRepaint()
        except Exception:  # noqa: BLE001 — stil är inte kritiskt
            pass

    def _status(self, text):
        self.status.setText(text)


# ---------------------------------------------------------------------------
# Plugin-livscykel
# ---------------------------------------------------------------------------


class DoaPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.dock = None
        self.action = None

    def initGui(self):  # noqa: N802
        # Utan ikon får QAction bara en tom knapp i verktygsfältet, vilket
        # i praktiken gör pluginet osynligt bland de andra. Sökvägen tas ur
        # __file__ eftersom QGIS laddar pluginet från sin egen katalog.
        ikon = QIcon(str(_Path(__file__).parent / "icon.png"))
        self.action = QAction(ikon, "Svensk data och analys",
                              self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.triggered.connect(self._vaxla)
        self.iface.addPluginToWebMenu("Svensk data och analys", self.action)
        self.iface.addToolBarIcon(self.action)

    def _vaxla(self, pa):
        if self.dock is None:
            self.dock = DoaDock(self.iface)
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
        self.dock.setVisible(pa)

    def unload(self):
        if self.dock is not None:
            self.iface.removeDockWidget(self.dock)
            self.dock = None
        if self.action is not None:
            self.iface.removeToolBarIcon(self.action)
            self.iface.removePluginWebMenu("Svensk data och analys", self.action)
