# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
"""Tests for kicad_ci.color_theme (SI-3)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_ci.color_theme import (
    ALTIUM_PALETTE,
    DNP_PALETTE,
    MONO_PALETTE,
    ColorTheme,
    ThemeManager,
    _kicad_config_dir,
    _layer_key,
    install_theme,
    merge,
    theme_from_palette,
)


# ---------------------------------------------------------------------------
# _layer_key
# ---------------------------------------------------------------------------

class TestLayerKey:
    def test_dot_replaced(self):
        assert _layer_key("F.Cu") == "f_cu"

    def test_all_lower(self):
        assert _layer_key("B.SilkS") == "b_silks"

    def test_hyphen_replaced(self):
        assert _layer_key("User-1") == "user_1"

    def test_already_lower(self):
        assert _layer_key("edge.cuts") == "edge_cuts"

    def test_inner_copper(self):
        assert _layer_key("In1.Cu") == "in1_cu"


# ---------------------------------------------------------------------------
# Palettes
# ---------------------------------------------------------------------------

class TestPalettes:
    def test_altium_has_front_copper(self):
        assert "F.Cu" in ALTIUM_PALETTE

    def test_altium_has_back_copper(self):
        assert "B.Cu" in ALTIUM_PALETTE

    def test_altium_color_format(self):
        for layer, color in ALTIUM_PALETTE.items():
            assert color.startswith("#"), f"{layer}: {color!r} not #RRGGBBAA"
            assert len(color) == 9, f"{layer}: {color!r} wrong length (expected #RRGGBBAA)"

    def test_dnp_palette_has_dnp_layers(self):
        assert "User.DNP.Top" in DNP_PALETTE
        assert "User.DNP.Bot" in DNP_PALETTE

    def test_mono_palette_front_copper_is_black(self):
        assert MONO_PALETTE["F.Cu"] == "#000000FF"

    def test_mono_palette_back_copper_is_black(self):
        assert MONO_PALETTE["B.Cu"] == "#000000FF"


# ---------------------------------------------------------------------------
# merge()
# ---------------------------------------------------------------------------

class TestMerge:
    def test_override_replaces_key(self):
        base = {"F.Cu": "#FF0000FF", "B.Cu": "#0000FFFF"}
        override = {"F.Cu": "#00FF00FF"}
        result = merge(base, override)
        assert result["F.Cu"] == "#00FF00FF"
        assert result["B.Cu"] == "#0000FFFF"

    def test_override_adds_new_key(self):
        base = {"F.Cu": "#FF0000FF"}
        override = {"User.DNP.Top": "#808080FF"}
        result = merge(base, override)
        assert "User.DNP.Top" in result
        assert "F.Cu" in result

    def test_base_not_mutated(self):
        base = {"F.Cu": "#FF0000FF"}
        merge(base, {"F.Cu": "#00FF00FF"})
        assert base["F.Cu"] == "#FF0000FF"

    def test_dnp_over_altium(self):
        combined = merge(ALTIUM_PALETTE, DNP_PALETTE)
        assert "User.DNP.Top" in combined
        assert "F.Cu" in combined


# ---------------------------------------------------------------------------
# ColorTheme dataclass
# ---------------------------------------------------------------------------

class TestColorTheme:
    def test_to_dict_has_meta(self):
        t = ColorTheme(name="test")
        d = t.to_dict()
        assert "meta" in d
        assert d["meta"]["filename"] == "test.json"

    def test_to_dict_has_board_section(self):
        t = ColorTheme(name="test")
        d = t.to_dict()
        assert "board" in d

    def test_to_dict_has_schematic_section(self):
        t = ColorTheme(name="test")
        d = t.to_dict()
        assert "schematic" in d

    def test_layer_name_converted_in_board(self):
        t = ColorTheme(name="test", board={"F.Cu": "#FF0000FF"})
        d = t.to_dict()
        assert "f_cu" in d["board"]
        assert d["board"]["f_cu"] == "#FF0000FF"

    def test_kicad8_schema_version_5(self):
        t = ColorTheme(name="test", kicad_version=8)
        assert t.to_dict()["meta"]["version"] == 5

    def test_kicad7_schema_version_4(self):
        t = ColorTheme(name="test", kicad_version=7)
        assert t.to_dict()["meta"]["version"] == 4

    def test_to_json_is_valid_json(self):
        t = ColorTheme(name="test", board=ALTIUM_PALETTE)
        parsed = json.loads(t.to_json())
        assert "board" in parsed

    def test_to_json_contains_f_cu(self):
        t = ColorTheme(name="test", board={"F.Cu": "#DC2828FF"})
        parsed = json.loads(t.to_json())
        assert parsed["board"]["f_cu"] == "#DC2828FF"

    def test_default_board_ui_present(self):
        t = ColorTheme(name="test")
        d = t.to_dict()
        assert "background" in d["board"]
        assert "anchor" in d["board"]

    def test_default_sch_ui_present(self):
        t = ColorTheme(name="test")
        d = t.to_dict()
        assert "background" in d["schematic"]
        assert "wire" in d["schematic"]

    def test_custom_sch_colors_override_defaults(self):
        t = ColorTheme(name="test", schematic={"background": "#000000FF"})
        d = t.to_dict()
        assert d["schematic"]["background"] == "#000000FF"


# ---------------------------------------------------------------------------
# theme_from_palette()
# ---------------------------------------------------------------------------

class TestThemeFromPalette:
    def test_creates_color_theme(self):
        t = theme_from_palette("altium", ALTIUM_PALETTE)
        assert isinstance(t, ColorTheme)
        assert t.name == "altium"

    def test_board_palette_stored(self):
        t = theme_from_palette("mono", MONO_PALETTE)
        assert t.board["F.Cu"] == MONO_PALETTE["F.Cu"]

    def test_sch_palette_optional(self):
        t = theme_from_palette("test", ALTIUM_PALETTE)
        assert t.schematic == {}

    def test_sch_palette_stored_when_given(self):
        sch = {"background": "#FFFFFFFF"}
        t = theme_from_palette("test", ALTIUM_PALETTE, sch_palette=sch)
        assert t.schematic["background"] == "#FFFFFFFF"

    def test_kicad_version_forwarded(self):
        t = theme_from_palette("test", ALTIUM_PALETTE, kicad_version=7)
        assert t.kicad_version == 7


# ---------------------------------------------------------------------------
# _kicad_config_dir()
# ---------------------------------------------------------------------------

class TestKicadConfigDir:
    def test_env_var_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path))
        result = _kicad_config_dir(8)
        assert str(tmp_path) in str(result)
        assert "8.0" in str(result)
        assert "colors" in str(result)

    def test_version_in_path(self, monkeypatch):
        monkeypatch.setenv("KICAD_CONFIG_HOME", "/tmp/cfg")
        assert "7.0" in str(_kicad_config_dir(7))
        assert "8.0" in str(_kicad_config_dir(8))

    def test_no_env_var_returns_path(self, monkeypatch):
        monkeypatch.delenv("KICAD_CONFIG_HOME", raising=False)
        result = _kicad_config_dir(8)
        assert result.name == "colors"


# ---------------------------------------------------------------------------
# ThemeManager context manager
# ---------------------------------------------------------------------------

class TestThemeManager:
    def test_writes_json_on_enter(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path))
        theme = ColorTheme(name="ci_test", board={"F.Cu": "#FF0000FF"})
        mgr = ThemeManager(theme)
        name = mgr.__enter__()
        try:
            assert name.startswith("ci_test_")
            written = list(tmp_path.rglob("*.json"))
            assert len(written) == 1
            data = json.loads(written[0].read_text())
            assert data["board"]["f_cu"] == "#FF0000FF"
        finally:
            mgr.__exit__(None, None, None)

    def test_removes_file_on_exit(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path))
        theme = ColorTheme(name="ci_test", board={})
        mgr = ThemeManager(theme)
        mgr.__enter__()
        mgr.__exit__(None, None, None)
        assert list(tmp_path.rglob("*.json")) == []

    def test_removes_file_on_exception(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path))
        theme = ColorTheme(name="ci_test", board={})
        mgr = ThemeManager(theme)
        mgr.__enter__()
        # Simulate exception in with-block
        mgr.__exit__(RuntimeError, RuntimeError("boom"), None)
        assert list(tmp_path.rglob("*.json")) == []

    def test_unique_names_no_collision(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path))
        theme = ColorTheme(name="ci", board={})
        m1 = ThemeManager(theme)
        m2 = ThemeManager(theme)
        n1 = m1.__enter__()
        n2 = m2.__enter__()
        try:
            assert n1 != n2
        finally:
            m1.__exit__(None, None, None)
            m2.__exit__(None, None, None)

    def test_creates_config_dir_if_missing(self, tmp_path, monkeypatch):
        deep = tmp_path / "new" / "path"
        monkeypatch.setenv("KICAD_CONFIG_HOME", str(deep))
        theme = ColorTheme(name="t", board={})
        mgr = ThemeManager(theme)
        name = mgr.__enter__()
        mgr.__exit__(None, None, None)
        # If we get here without exception, dir was created

    def test_install_theme_context_manager(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path))
        theme = ColorTheme(name="ctx_test", board={"B.Cu": "#0000FFFF"})
        with install_theme(theme) as name:
            assert name.startswith("ctx_test_")
            files = list(tmp_path.rglob("*.json"))
            assert len(files) == 1
        # File removed after context exit
        assert list(tmp_path.rglob("*.json")) == []
