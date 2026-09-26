# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""QGIS-plugin: Svensk data och analys — sök statistik och visa på karta."""


def classFactory(iface):  # noqa: N802 (QGIS-konvention)
    from .doa_plugin import DoaPlugin

    return DoaPlugin(iface)
