# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
"""Tests for kicad_ci.kicad_cli — subprocess wrapper."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kicad_ci.kicad_cli import (
    KiCadCLI,
    KiCadCLIError,
    KiCadCLINotFoundError,
    KiCadCLITimeoutError,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_result(stdout="", stderr="", returncode=0):
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.stdout = stdout
    r.stderr = stderr
    r.returncode = returncode
    return r


def _cli_with_mock(monkeypatch, mock_run=None):
    """Return a KiCadCLI with a fake executable and mocked subprocess.run."""
    cli = KiCadCLI.__new__(KiCadCLI)
    cli._executable = "/fake/kicad-cli"
    cli.timeout = 300
    cli.extra_env = {}
    cli._version_cache = None
    import threading
    cli._version_lock = threading.Lock()

    if mock_run is not None:
        monkeypatch.setattr(subprocess, "run", mock_run)
    return cli


# ---------------------------------------------------------------------------
# Executable resolution
# ---------------------------------------------------------------------------

class TestExecutableResolution:
    def test_explicit_path_valid(self, tmp_path):
        fake = tmp_path / "kicad-cli"
        fake.write_text("#!/bin/sh\necho 8.0.3")
        cli = KiCadCLI(executable=str(fake))
        assert cli.executable == str(fake)

    def test_explicit_path_invalid_raises(self, tmp_path):
        with pytest.raises(KiCadCLINotFoundError):
            KiCadCLI(executable=str(tmp_path / "nonexistent"))

    def test_not_found_on_path_raises(self):
        with patch("shutil.which", return_value=None), \
             patch("os.path.isfile", return_value=False):
            with pytest.raises(KiCadCLINotFoundError):
                KiCadCLI()

    def test_found_on_system_path(self):
        with patch("shutil.which", return_value="/usr/bin/kicad-cli"), \
             patch("os.path.isfile", return_value=True):
            cli = KiCadCLI()
            assert cli.executable == "/usr/bin/kicad-cli"

    def test_found_at_fallback_path(self):
        with patch("shutil.which", return_value=None), \
             patch("os.path.isfile", side_effect=lambda p: "KiCad" in p):
            # Should find the first Windows/Mac fallback that isfile returns True
            cli = KiCadCLI()
            assert "kicad-cli" in cli.executable.lower() or "KiCad" in cli.executable


# ---------------------------------------------------------------------------
# Version detection
# ---------------------------------------------------------------------------

class TestVersion:
    def test_version_parsed_from_stdout(self, monkeypatch):
        cli = _cli_with_mock(monkeypatch,
            mock_run=lambda *a, **kw: _make_result(stdout="8.0.3\n"))
        assert cli.version() == "8.0.3"

    def test_version_parsed_from_verbose_output(self, monkeypatch):
        out = "Application: kicad-cli\nVersion: 7.0.10, release build\n"
        cli = _cli_with_mock(monkeypatch,
            mock_run=lambda *a, **kw: _make_result(stdout=out))
        assert cli.version() == "7.0.10"

    def test_version_cached(self, monkeypatch):
        call_count = 0

        def fake_run(*a, **kw):
            nonlocal call_count
            call_count += 1
            return _make_result(stdout="8.0.3\n")

        cli = _cli_with_mock(monkeypatch, mock_run=fake_run)
        cli.version()
        cli.version()
        assert call_count == 1

    def test_version_tuple(self, monkeypatch):
        cli = _cli_with_mock(monkeypatch,
            mock_run=lambda *a, **kw: _make_result(stdout="8.0.3\n"))
        assert cli.version_tuple() == (8, 0, 3)


# ---------------------------------------------------------------------------
# Core runner
# ---------------------------------------------------------------------------

class TestRun:
    def test_successful_run_returns_completedprocess(self, monkeypatch):
        cli = _cli_with_mock(monkeypatch,
            mock_run=lambda *a, **kw: _make_result(stdout="ok"))
        result = cli.run(["--version"])
        assert result.stdout == "ok"

    def test_nonzero_exit_raises_kicadclierror(self, monkeypatch):
        cli = _cli_with_mock(monkeypatch,
            mock_run=lambda *a, **kw: _make_result(returncode=1, stderr="error"))
        with pytest.raises(KiCadCLIError) as exc_info:
            cli.run(["bad", "args"])
        assert exc_info.value.returncode == 1
        assert "error" in exc_info.value.stderr

    def test_check_false_no_raise_on_nonzero(self, monkeypatch):
        cli = _cli_with_mock(monkeypatch,
            mock_run=lambda *a, **kw: _make_result(returncode=1))
        result = cli.run(["bad"], check=False)
        assert result.returncode == 1

    def test_timeout_raises_kicadclitimeouterror(self, monkeypatch):
        def fake_run(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="kicad-cli", timeout=1)

        cli = _cli_with_mock(monkeypatch, mock_run=fake_run)
        with pytest.raises(KiCadCLITimeoutError):
            cli.run(["slow"])

    def test_file_not_found_raises_kicadclinotfounderror(self, monkeypatch):
        def fake_run(*a, **kw):
            raise FileNotFoundError("no such file")

        cli = _cli_with_mock(monkeypatch, mock_run=fake_run)
        with pytest.raises(KiCadCLINotFoundError):
            cli.run(["something"])

    def test_cmd_includes_executable_and_args(self, monkeypatch):
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return _make_result()

        cli = _cli_with_mock(monkeypatch, mock_run=fake_run)
        cli.run(["pcb", "export", "gerbers"])
        assert captured["cmd"][0] == "/fake/kicad-cli"
        assert captured["cmd"][1:] == ["pcb", "export", "gerbers"]

    def test_extra_env_passed_to_subprocess(self, monkeypatch):
        captured = {}

        def fake_run(cmd, **kw):
            captured["env"] = kw.get("env")
            return _make_result()

        cli = _cli_with_mock(monkeypatch, mock_run=fake_run)
        cli.extra_env = {"MY_VAR": "hello"}
        cli.run(["--version"])
        assert captured["env"]["MY_VAR"] == "hello"

    def test_per_call_timeout_overrides_default(self, monkeypatch):
        captured = {}

        def fake_run(cmd, **kw):
            captured["timeout"] = kw.get("timeout")
            return _make_result()

        cli = _cli_with_mock(monkeypatch, mock_run=fake_run)
        cli.run(["--version"], timeout=42)
        assert captured["timeout"] == 42

    def test_error_str_includes_cmd_and_stderr(self):
        err = KiCadCLIError("failed", returncode=2, stderr="bad input", cmd=["kicad-cli", "drc"])
        s = str(err)
        assert "kicad-cli" in s
        assert "bad input" in s
        assert "2" in s


# ---------------------------------------------------------------------------
# PCB export argument building
# ---------------------------------------------------------------------------

class TestPcbExports:
    def _capture_args(self, monkeypatch):
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return _make_result()

        cli = _cli_with_mock(monkeypatch, mock_run=fake_run)
        return cli, captured

    def test_pcb_export_gerbers_basic(self, monkeypatch):
        cli, cap = self._capture_args(monkeypatch)
        cli.pcb_export_gerbers("/a/b.kicad_pcb", "/out")
        assert "gerbers" in cap["cmd"]
        assert "/out" in cap["cmd"]
        assert "/a/b.kicad_pcb" in cap["cmd"]

    def test_pcb_export_gerbers_with_layers(self, monkeypatch):
        cli, cap = self._capture_args(monkeypatch)
        cli.pcb_export_gerbers("/a/b.kicad_pcb", "/out", layers=["F.Cu", "B.Cu"])
        assert "F.Cu,B.Cu" in cap["cmd"]

    def test_pcb_export_gerbers_no_x2(self, monkeypatch):
        cli, cap = self._capture_args(monkeypatch)
        cli.pcb_export_gerbers("/a/b.kicad_pcb", "/out", no_x2=True)
        assert "--no-x2" in cap["cmd"]

    def test_pcb_export_drill(self, monkeypatch):
        cli, cap = self._capture_args(monkeypatch)
        cli.pcb_export_drill("/a/b.kicad_pcb", "/out")
        assert "drill" in cap["cmd"]
        assert "--format" in cap["cmd"]

    def test_pcb_export_pos_excludes_dnp(self, monkeypatch):
        cli, cap = self._capture_args(monkeypatch)
        cli.pcb_export_pos("/a/b.kicad_pcb", "/out/pos.csv", exclude_dnp=True)
        assert "--exclude-dnp" in cap["cmd"]

    def test_pcb_export_svg(self, monkeypatch):
        cli, cap = self._capture_args(monkeypatch)
        cli.pcb_export_svg("/a/b.kicad_pcb", "/out", theme="dark")
        assert "--theme" in cap["cmd"]
        assert "dark" in cap["cmd"]

    def test_pcb_export_step_force(self, monkeypatch):
        cli, cap = self._capture_args(monkeypatch)
        cli.pcb_export_step("/a/b.kicad_pcb", "/out/board.step")
        assert "--force" in cap["cmd"]

    def test_pcb_drc_format_json(self, monkeypatch):
        cli, cap = self._capture_args(monkeypatch)
        cli.pcb_drc("/a/b.kicad_pcb", "/out/drc.json")
        assert "--format" in cap["cmd"]
        assert "json" in cap["cmd"]


# ---------------------------------------------------------------------------
# Schematic export argument building
# ---------------------------------------------------------------------------

class TestSchExports:
    def _capture_args(self, monkeypatch):
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return _make_result()

        cli = _cli_with_mock(monkeypatch, mock_run=fake_run)
        return cli, captured

    def test_sch_export_pdf(self, monkeypatch):
        cli, cap = self._capture_args(monkeypatch)
        cli.sch_export_pdf("/a/b.kicad_sch", "/out/sch.pdf")
        assert "pdf" in cap["cmd"]
        assert "/out/sch.pdf" in cap["cmd"]

    def test_sch_export_svg(self, monkeypatch):
        cli, cap = self._capture_args(monkeypatch)
        cli.sch_export_svg("/a/b.kicad_sch", "/out")
        assert "svg" in cap["cmd"]

    def test_sch_export_netlist_format(self, monkeypatch):
        cli, cap = self._capture_args(monkeypatch)
        cli.sch_export_netlist("/a/b.kicad_sch", "/out/net.xml", format="spice")
        assert "spice" in cap["cmd"]

    def test_sch_export_bom_exclude_dnp(self, monkeypatch):
        cli, cap = self._capture_args(monkeypatch)
        cli.sch_export_bom("/a/b.kicad_sch", "/out/bom.csv", exclude_dnp=True)
        assert "--exclude-dnp" in cap["cmd"]

    def test_sch_export_bom_group_by(self, monkeypatch):
        cli, cap = self._capture_args(monkeypatch)
        cli.sch_export_bom("/a/b.kicad_sch", "/out/bom.csv", group_by="Value,Footprint")
        assert "Value,Footprint" in cap["cmd"]

    def test_sch_erc_format_json(self, monkeypatch):
        cli, cap = self._capture_args(monkeypatch)
        cli.sch_erc("/a/b.kicad_sch", "/out/erc.json")
        assert "--format" in cap["cmd"]
        assert "json" in cap["cmd"]

    def test_sch_export_pythonbom(self, monkeypatch):
        cli, cap = self._capture_args(monkeypatch)
        cli.sch_export_pythonbom("/a/b.kicad_sch", "/out/bom.py")
        assert "python-bom" in cap["cmd"]
