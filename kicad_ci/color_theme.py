# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
"""
KiCad color theme generator (SI-3).

Programmatically builds KiCad color-theme JSON files for use with
kicad-cli --theme.  Supports Altium-style, DNP-highlight, and
monochrome-fab palettes.  Includes a context-manager that installs the
theme into KiCad's config directory for the duration of an export and
removes it afterwards.

Public API
----------
    ColorTheme(name, board, schematic)
        Dataclass holding per-category color maps.
    theme_from_palette(name, board_palette, sch_palette=None)
        Build a ColorTheme from flat layer→color dicts.
    ALTIUM_PALETTE, DNP_PALETTE, MONO_PALETTE, INVISIBLE
        Pre-defined board palettes.
    merge(base, override)
        Overlay one palette on another (returns new dict).
    ThemeManager(theme)
        Context manager that writes/removes the theme JSON.
"""

from __future__ import annotations

import json
import os
import platform
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Generator, Iterator, Optional


# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------

def _layer_key(name: str) -> str:
    """Convert a KiCad layer name to its JSON key (lower, dots→underscores)."""
    return name.lower().replace(".", "_").replace("-", "_")


# ---------------------------------------------------------------------------
# Palette constants  (colors as "#RRGGBBAA")
# ---------------------------------------------------------------------------

# Altium-style palette — vibrant, industry-familiar colours
ALTIUM_PALETTE: Dict[str, str] = {
    "F.Cu":       "#DC2828FF",
    "B.Cu":       "#2828DCFF",
    "In1.Cu":     "#FFFF00FF",
    "In2.Cu":     "#00FF00FF",
    "In3.Cu":     "#FF8000FF",
    "In4.Cu":     "#FF00FFFF",
    "In5.Cu":     "#00FFFFFF",
    "In6.Cu":     "#804000FF",
    "F.Adhes":    "#800000C8",
    "B.Adhes":    "#A000A0C8",
    "F.Paste":    "#808080C8",
    "B.Paste":    "#404040C8",
    "F.SilkS":    "#E0E0E0FF",
    "B.SilkS":    "#A0A0A0FF",
    "F.Mask":     "#FF000064",
    "B.Mask":     "#0000FF64",
    "Dwgs.User":  "#E0E0E0C8",
    "Cmts.User":  "#7F7F7FC8",
    "Eco1.User":  "#00FF00C8",
    "Eco2.User":  "#00E0E0C8",
    "Edge.Cuts":  "#FFFF00FF",
    "F.CrtYd":    "#FF26E2FF",
    "B.CrtYd":    "#C2C200FF",
    "F.Fab":      "#FF8000C8",
    "B.Fab":      "#0066FFFF",
    "User.1":     "#CC9900FF",
    "User.2":     "#00CC99FF",
    "User.3":     "#9900CCFF",
    "User.4":     "#CC0099FF",
    "User.5":     "#00CCCCFF",
    "User.6":     "#CCCC00FF",
    "User.7":     "#00CC00FF",
    "User.8":     "#CC0000FF",
    "User.9":     "#0000CCFF",
}

# DNP-highlight additions — applied *over* ALTIUM_PALETTE via merge()
DNP_PALETTE: Dict[str, str] = {
    "User.DNP.Top": "#808080FF",
    "User.DNP.Bot": "#606060FF",
}

# Monochrome fabrication palette — black on white, all copper black
MONO_PALETTE: Dict[str, str] = {
    "F.Cu":    "#000000FF",
    "B.Cu":    "#000000FF",
    "In1.Cu":  "#000000FF",
    "In2.Cu":  "#000000FF",
    "In3.Cu":  "#000000FF",
    "In4.Cu":  "#000000FF",
    "In5.Cu":  "#000000FF",
    "In6.Cu":  "#000000FF",
    "F.SilkS": "#000000FF",
    "B.SilkS": "#000000FF",
    "F.Fab":   "#000000FF",
    "B.Fab":   "#000000FF",
    "F.Mask":  "#00000064",
    "B.Mask":  "#00000064",
    "F.Paste": "#000000C8",
    "B.Paste": "#000000C8",
    "F.CrtYd": "#000000FF",
    "B.CrtYd": "#000000FF",
    "Edge.Cuts": "#000000FF",
    "Dwgs.User": "#000000C8",
}

# Fully transparent — useful as base for single-layer exports
INVISIBLE: Dict[str, str] = {}


def merge(base: Dict[str, str], override: Dict[str, str]) -> Dict[str, str]:
    """
    Return a new palette combining *base* with *override* applied on top.

    Keys in *override* replace or extend those in *base*.
    """
    result = dict(base)
    result.update(override)
    return result


# ---------------------------------------------------------------------------
# Theme dataclass
# ---------------------------------------------------------------------------

# Default board-level non-layer colours (UI chrome, anchors, etc.)
_DEFAULT_BOARD_UI: Dict[str, str] = {
    "anchor":             "#FF6464FF",
    "aux_items":          "#C8C8C8FF",
    "background":         "#212121FF",
    "cursor":             "#FFFFFFFF",
    "grid":               "#3C3C3CFF",
    "grid_axes":          "#4B4B4BFF",
    "no_connect":         "#FF0000FF",
    "pad_plated_hole":    "#C8C878FF",
    "pad_through_hole":   "#C2C200FF",
    "ratsnest":           "#FFFFFFFF",
    "select_overlay":     "#4088FFAA",
    "via_blind_buried":   "#C8A870FF",
    "via_micro":          "#FFAAFF00",
    "via_through":        "#C8C800FF",
    "worksheet":          "#FF0000FF",
    "copper_text":        "#808080FF",
    "copper_wire":        "#808080FF",
}

_DEFAULT_SCH_UI: Dict[str, str] = {
    "background":       "#FFFFFFFF",
    "bus":              "#00009CFF",
    "bus_junction":     "#00009CFF",
    "component_body":   "#FFFFC0FF",
    "component_outline":"#840000FF",
    "cursor":           "#000000FF",
    "erc_error":        "#F00000FF",
    "erc_warning":      "#FF8000FF",
    "fields":           "#840000FF",
    "grid":             "#9A9A9AFF",
    "hidden":           "#808080FF",
    "junction":         "#009900FF",
    "label_global":     "#BF0000FF",
    "label_hier":       "#008000FF",
    "label_local":      "#000000FF",
    "net_color_demo":   "#FF0000FF",
    "no_connect":       "#000000FF",
    "note":             "#0000D4FF",
    "override_item_colors": "#808080FF",
    "pin":              "#840000FF",
    "pin_hidden":       "#808080FF",
    "reference":        "#840000FF",
    "shadow":           "#B4B4B4FF",
    "sheet":            "#000000FF",
    "sheet_background": "#FFFFE0D8",
    "sheet_filename":   "#BD2C00FF",
    "sheet_fields":     "#006400FF",
    "sheet_label":      "#000000FF",
    "value":            "#840000FF",
    "wire":             "#00009CFF",
    "worksheet":        "#C8000000",
    "pin_dangling":     "#FF0000FF",
    "label_unconnected":"#FF0000FF",
}


@dataclass
class ColorTheme:
    """
    A KiCad color theme.

    Parameters
    ----------
    name:
        Theme name (used as the JSON filename stem and --theme argument).
    board:
        Flat dict of KiCad layer names / board-UI keys → ``"#RRGGBBAA"`` colors.
    schematic:
        Flat dict of schematic-UI keys → ``"#RRGGBBAA"`` colors.
    kicad_version:
        Major KiCad version (7 or 8).  Affects JSON schema version number.
    """

    name: str
    board: Dict[str, str] = field(default_factory=dict)
    schematic: Dict[str, str] = field(default_factory=dict)
    kicad_version: int = 8

    def _build_board_section(self) -> Dict[str, str]:
        result = dict(_DEFAULT_BOARD_UI)
        for layer, color in self.board.items():
            result[_layer_key(layer)] = color
        return result

    def _build_sch_section(self) -> Dict[str, str]:
        result = dict(_DEFAULT_SCH_UI)
        for k, v in self.schematic.items():
            result[k] = v
        return result

    def to_dict(self) -> dict:
        """Return the full JSON-ready dict."""
        schema_version = 5 if self.kicad_version >= 8 else 4
        return {
            "meta": {
                "filename": f"{self.name}.json",
                "version": schema_version,
            },
            "board": self._build_board_section(),
            "schematic": self._build_sch_section(),
        }

    def to_json(self, indent: int = 2) -> str:
        """Render as a JSON string suitable for writing to a .json theme file."""
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)


def theme_from_palette(
    name: str,
    board_palette: Dict[str, str],
    sch_palette: Optional[Dict[str, str]] = None,
    kicad_version: int = 8,
) -> ColorTheme:
    """
    Build a :class:`ColorTheme` from flat layer→color dicts.

    Parameters
    ----------
    name:
        Theme name.
    board_palette:
        Mapping of KiCad layer names to ``"#RRGGBBAA"`` colors.
    sch_palette:
        Optional schematic colors (uses defaults if None).
    kicad_version:
        Target KiCad major version.
    """
    return ColorTheme(
        name=name,
        board=dict(board_palette),
        schematic=dict(sch_palette) if sch_palette else {},
        kicad_version=kicad_version,
    )


# ---------------------------------------------------------------------------
# Config path resolution
# ---------------------------------------------------------------------------

def _kicad_config_dir(kicad_version: int = 8) -> Path:
    """
    Return the KiCad per-version config directory.

    Checks KICAD_CONFIG_HOME first; falls back to platform defaults:
      Linux/macOS: ~/.config/kicad/{version}.0/
      Windows:     %APPDATA%/kicad/{version}.0/
    """
    env_home = os.environ.get("KICAD_CONFIG_HOME")
    if env_home:
        base = Path(env_home)
    elif platform.system() == "Windows":
        appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        base = Path(appdata) / "kicad"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        base = Path(xdg) / "kicad"

    return base / f"{kicad_version}.0" / "colors"


# ---------------------------------------------------------------------------
# Theme context manager
# ---------------------------------------------------------------------------

class ThemeManager:
    """
    Context manager that installs a :class:`ColorTheme` into KiCad's
    config directory for the duration of the ``with`` block.

    Usage::

        with ThemeManager(my_theme) as theme_name:
            cli.pcb_export_pdf(board, "out.pdf", theme=theme_name)

    A UUID suffix is appended to the theme name so concurrent CI jobs
    don't collide.  The file is always removed in ``__exit__``, even if
    an exception is raised.
    """

    def __init__(self, theme: ColorTheme):
        self._theme = theme
        self._path: Optional[Path] = None
        self._theme_name: Optional[str] = None

    def __enter__(self) -> str:
        suffix = uuid.uuid4().hex[:8]
        self._theme_name = f"{self._theme.name}_{suffix}"
        colors_dir = _kicad_config_dir(self._theme.kicad_version)
        colors_dir.mkdir(parents=True, exist_ok=True)
        self._path = colors_dir / f"{self._theme_name}.json"
        # Build a copy with the unique name so the JSON filename field matches
        themed = ColorTheme(
            name=self._theme_name,
            board=self._theme.board,
            schematic=self._theme.schematic,
            kicad_version=self._theme.kicad_version,
        )
        self._path.write_text(themed.to_json(), encoding="utf-8")
        return self._theme_name

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._path and self._path.exists():
            try:
                self._path.unlink()
            except OSError:
                pass
        return None  # do not suppress exceptions


@contextmanager
def install_theme(theme: ColorTheme) -> Generator[str, None, None]:
    """
    Convenience context-manager wrapping :class:`ThemeManager`.

    Yields the unique theme name to pass to ``--theme``.
    """
    mgr = ThemeManager(theme)
    name = mgr.__enter__()
    try:
        yield name
    finally:
        mgr.__exit__(None, None, None)
