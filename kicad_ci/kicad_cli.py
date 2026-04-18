# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
"""
kicad-cli subprocess wrapper.

Wraps all kicad-cli subcommands with:
  - Structured argument building
  - Timeout enforcement
  - Stderr/stdout capture and logging
  - Version detection and caching
  - Meaningful error types

Usage
-----
    from kicad_ci.kicad_cli import KiCadCLI, KiCadCLIError

    cli = KiCadCLI()                        # auto-detects kicad-cli on PATH
    cli = KiCadCLI(executable="/usr/bin/kicad-cli")

    ver = cli.version()                     # e.g. "8.0.3"

    # PCB exports
    cli.pcb_export_gerbers(pcb, output_dir)
    cli.pcb_export_drill(pcb, output_dir)
    cli.pcb_export_pos(pcb, output_file)
    cli.pcb_export_svg(pcb, output_dir)
    cli.pcb_export_pdf(pcb, output_file)
    cli.pcb_export_dxf(pcb, output_dir)
    cli.pcb_export_step(pcb, output_file)
    cli.pcb_export_3d(pcb, output_file, format="glb")
    cli.pcb_drc(pcb, output_file)

    # Schematic exports
    cli.sch_export_pdf(sch, output_file)
    cli.sch_export_svg(sch, output_dir)
    cli.sch_export_netlist(sch, output_file)
    cli.sch_export_bom(sch, output_file)
    cli.sch_export_pythonbom(sch, output_file)
    cli.sch_erc(sch, output_file)

    # Generic escape hatch
    cli.run(["pcb", "export", "gerbers", ...])
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Sequence

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 300  # seconds


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class KiCadCLIError(RuntimeError):
    """Raised when kicad-cli exits non-zero or cannot be found."""

    def __init__(
        self,
        message: str,
        returncode: int | None = None,
        stdout: str = "",
        stderr: str = "",
        cmd: list[str] | None = None,
    ):
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.cmd = cmd or []

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.cmd:
            parts.append(f"  cmd: {' '.join(self.cmd)}")
        if self.returncode is not None:
            parts.append(f"  exit: {self.returncode}")
        if self.stderr.strip():
            parts.append(f"  stderr: {self.stderr.strip()[:500]}")
        return "\n".join(parts)


class KiCadCLINotFoundError(KiCadCLIError):
    """Raised when kicad-cli executable cannot be located."""


class KiCadCLITimeoutError(KiCadCLIError):
    """Raised when a kicad-cli invocation exceeds the timeout."""


# ---------------------------------------------------------------------------
# Core wrapper
# ---------------------------------------------------------------------------

class KiCadCLI:
    """
    Wrapper around the ``kicad-cli`` command-line tool.

    Parameters
    ----------
    executable:
        Path to kicad-cli binary. If None, searches PATH and common
        platform install locations.
    timeout:
        Default per-command timeout in seconds (default 300).
    extra_env:
        Extra environment variables merged into the subprocess env.
    """

    # Common install paths by platform
    _SEARCH_PATHS: list[str] = [
        # Linux / macOS
        "/usr/bin/kicad-cli",
        "/usr/local/bin/kicad-cli",
        "/opt/kicad/bin/kicad-cli",
        # macOS app bundle
        "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
        # Windows
        r"C:\Program Files\KiCad\8.0\bin\kicad-cli.exe",
        r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe",
        r"C:\Program Files\KiCad\7.0\bin\kicad-cli.exe",
    ]

    def __init__(
        self,
        executable: str | Path | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        extra_env: dict[str, str] | None = None,
    ):
        self.timeout = timeout
        self.extra_env = extra_env or {}
        self._executable: str = self._resolve_executable(executable)
        self._version_cache: str | None = None
        self._version_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Executable resolution
    # ------------------------------------------------------------------

    def _resolve_executable(self, hint: str | Path | None) -> str:
        if hint is not None:
            path = str(hint)
            if not os.path.isfile(path):
                raise KiCadCLINotFoundError(
                    f"kicad-cli not found at specified path: {path}"
                )
            return path

        # Search PATH first
        found = shutil.which("kicad-cli")
        if found:
            return found

        # Platform-specific fallbacks
        for candidate in self._SEARCH_PATHS:
            if os.path.isfile(candidate):
                return candidate

        raise KiCadCLINotFoundError(
            "kicad-cli not found on PATH or in common install locations. "
            "Install KiCad or set executable= explicitly."
        )

    @property
    def executable(self) -> str:
        return self._executable

    # ------------------------------------------------------------------
    # Version detection
    # ------------------------------------------------------------------

    def version(self) -> str:
        """
        Return the kicad-cli version string (e.g. ``"8.0.3"``).

        Result is cached after the first call.
        """
        with self._version_lock:
            if self._version_cache is None:
                result = self.run(["--version"], timeout=15)
                # kicad-cli --version outputs e.g. "8.0.3" or
                # "Application: kicad-cli\nVersion: 8.0.3, ..."
                match = re.search(r"\b(\d+\.\d+\.\d+)\b", result.stdout + result.stderr)
                self._version_cache = match.group(1) if match else result.stdout.strip()
            return self._version_cache

    def version_tuple(self) -> tuple[int, ...]:
        """Return version as a comparable tuple, e.g. ``(8, 0, 3)``."""
        return tuple(int(x) for x in self.version().split(".") if x.isdigit())

    # ------------------------------------------------------------------
    # Core runner
    # ------------------------------------------------------------------

    def run(
        self,
        args: Sequence[str],
        *,
        timeout: int | None = None,
        check: bool = True,
        cwd: str | Path | None = None,
    ) -> subprocess.CompletedProcess:
        """
        Run kicad-cli with the given argument list.

        Parameters
        ----------
        args:
            Arguments passed after the executable name.
        timeout:
            Override the instance default timeout (seconds).
        check:
            If True (default), raise KiCadCLIError on non-zero exit.
        cwd:
            Working directory for the subprocess.

        Returns
        -------
        subprocess.CompletedProcess with .stdout and .stderr as str.
        """
        cmd = [self._executable, *args]
        effective_timeout = timeout if timeout is not None else self.timeout

        env = os.environ.copy()
        env.update(self.extra_env)

        log.debug("kicad-cli: %s", " ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
                cwd=cwd,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise KiCadCLITimeoutError(
                f"kicad-cli timed out after {effective_timeout}s",
                cmd=cmd,
            ) from exc
        except FileNotFoundError as exc:
            raise KiCadCLINotFoundError(
                f"kicad-cli executable not found: {self._executable}",
                cmd=cmd,
            ) from exc

        if result.stdout:
            log.debug("kicad-cli stdout: %s", result.stdout[:1000])
        if result.stderr:
            log.debug("kicad-cli stderr: %s", result.stderr[:1000])

        if check and result.returncode != 0:
            raise KiCadCLIError(
                f"kicad-cli exited with code {result.returncode}",
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                cmd=cmd,
            )

        return result

    # ------------------------------------------------------------------
    # PCB exports
    # ------------------------------------------------------------------

    def pcb_export_gerbers(
        self,
        pcb: str | Path,
        output_dir: str | Path,
        *,
        layers: list[str] | None = None,
        no_x2: bool = False,
        no_netlist: bool = False,
        subtract_soldermask: bool = False,
        disable_aperture_macros: bool = False,
        use_drill_file_origin: bool = False,
        precision: int | None = None,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess:
        """Export Gerber files from a .kicad_pcb file."""
        args = ["pcb", "export", "gerbers",
                "--output", str(output_dir),
                str(pcb)]
        if layers:
            args += ["--layers", ",".join(layers)]
        if no_x2:
            args.append("--no-x2")
        if no_netlist:
            args.append("--no-netlist")
        if subtract_soldermask:
            args.append("--subtract-soldermask")
        if disable_aperture_macros:
            args.append("--disable-aperture-macros")
        if use_drill_file_origin:
            args.append("--use-drill-file-origin")
        if precision is not None:
            args += ["--precision", str(precision)]
        return self.run(args, timeout=timeout)

    def pcb_export_drill(
        self,
        pcb: str | Path,
        output_dir: str | Path,
        *,
        format: str = "excellon",
        drill_origin: str = "absolute",
        excellon_zeros_format: str | None = None,
        excellon_oval_format: str | None = None,
        excellon_units: str | None = None,
        generate_map: str | None = None,
        separate_th: bool = False,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess:
        """Export drill files (Excellon or Gerber X2) from a .kicad_pcb file."""
        args = ["pcb", "export", "drill",
                "--output", str(output_dir),
                "--format", format,
                "--drill-origin", drill_origin,
                str(pcb)]
        if excellon_zeros_format:
            args += ["--excellon-zeros-format", excellon_zeros_format]
        if excellon_oval_format:
            args += ["--excellon-oval-format", excellon_oval_format]
        if excellon_units:
            args += ["--excellon-units", excellon_units]
        if generate_map:
            args += ["--generate-map", generate_map]
        if separate_th:
            args.append("--separate-th")
        return self.run(args, timeout=timeout)

    def pcb_export_pos(
        self,
        pcb: str | Path,
        output_file: str | Path,
        *,
        side: str = "both",
        format: str = "ascii",
        units: str = "mm",
        bottom_negate_x: bool = False,
        use_drill_file_origin: bool = False,
        smd_only: bool = False,
        exclude_fp_th: bool = False,
        exclude_dnp: bool = False,
        gerber_board_edge: bool = False,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess:
        """Export component placement (pick-and-place) file."""
        args = ["pcb", "export", "pos",
                "--output", str(output_file),
                "--side", side,
                "--format", format,
                "--units", units,
                str(pcb)]
        if bottom_negate_x:
            args.append("--bottom-negate-x")
        if use_drill_file_origin:
            args.append("--use-drill-file-origin")
        if smd_only:
            args.append("--smd-only")
        if exclude_fp_th:
            args.append("--exclude-fp-th")
        if exclude_dnp:
            args.append("--exclude-dnp")
        if gerber_board_edge:
            args.append("--gerber-board-edge")
        return self.run(args, timeout=timeout)

    def pcb_export_svg(
        self,
        pcb: str | Path,
        output_dir: str | Path,
        *,
        layers: list[str] | None = None,
        theme: str | None = None,
        negative: bool = False,
        black_and_white: bool = False,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess:
        """Export SVG from a .kicad_pcb file."""
        args = ["pcb", "export", "svg",
                "--output", str(output_dir),
                str(pcb)]
        if layers:
            args += ["--layers", ",".join(layers)]
        if theme:
            args += ["--theme", theme]
        if negative:
            args.append("--negative")
        if black_and_white:
            args.append("--black-and-white")
        return self.run(args, timeout=timeout)

    def pcb_export_pdf(
        self,
        pcb: str | Path,
        output_file: str | Path,
        *,
        layers: list[str] | None = None,
        theme: str | None = None,
        black_and_white: bool = False,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess:
        """Export PDF from a .kicad_pcb file."""
        args = ["pcb", "export", "pdf",
                "--output", str(output_file),
                str(pcb)]
        if layers:
            args += ["--layers", ",".join(layers)]
        if theme:
            args += ["--theme", theme]
        if black_and_white:
            args.append("--black-and-white")
        return self.run(args, timeout=timeout)

    def pcb_export_dxf(
        self,
        pcb: str | Path,
        output_dir: str | Path,
        *,
        layers: list[str] | None = None,
        output_units: str = "mm",
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess:
        """Export DXF from a .kicad_pcb file."""
        args = ["pcb", "export", "dxf",
                "--output", str(output_dir),
                "--output-units", output_units,
                str(pcb)]
        if layers:
            args += ["--layers", ",".join(layers)]
        return self.run(args, timeout=timeout)

    def pcb_export_step(
        self,
        pcb: str | Path,
        output_file: str | Path,
        *,
        force: bool = True,
        origin: str | None = None,
        no_dnp: bool = False,
        subst_models: bool = False,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess:
        """Export STEP 3D model from a .kicad_pcb file."""
        args = ["pcb", "export", "step",
                "--output", str(output_file),
                str(pcb)]
        if force:
            args.append("--force")
        if origin:
            args += ["--origin", origin]
        if no_dnp:
            args.append("--no-dnp")
        if subst_models:
            args.append("--subst-models")
        return self.run(args, timeout=timeout)

    def pcb_export_3d(
        self,
        pcb: str | Path,
        output_file: str | Path,
        *,
        format: str = "glb",
        force: bool = True,
        no_dnp: bool = False,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess:
        """Export GLB/GLTF 3D model from a .kicad_pcb file."""
        args = ["pcb", "export", "glb",
                "--output", str(output_file),
                str(pcb)]
        if force:
            args.append("--force")
        if no_dnp:
            args.append("--no-dnp")
        return self.run(args, timeout=timeout)

    def pcb_drc(
        self,
        pcb: str | Path,
        output_file: str | Path,
        *,
        format: str = "json",
        all_track_errors: bool = False,
        schematic_parity: bool = False,
        units: str = "mm",
        severity_all: bool = False,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess:
        """Run DRC on a .kicad_pcb file and write report."""
        args = ["pcb", "drc",
                "--output", str(output_file),
                "--format", format,
                "--units", units,
                str(pcb)]
        if all_track_errors:
            args.append("--all-track-errors")
        if schematic_parity:
            args.append("--schematic-parity")
        if severity_all:
            args.append("--severity-all")
        return self.run(args, timeout=timeout)

    # ------------------------------------------------------------------
    # Schematic exports
    # ------------------------------------------------------------------

    def sch_export_pdf(
        self,
        sch: str | Path,
        output_file: str | Path,
        *,
        theme: str | None = None,
        black_and_white: bool = False,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess:
        """Export PDF from a .kicad_sch file."""
        args = ["sch", "export", "pdf",
                "--output", str(output_file),
                str(sch)]
        if theme:
            args += ["--theme", theme]
        if black_and_white:
            args.append("--black-and-white")
        return self.run(args, timeout=timeout)

    def sch_export_svg(
        self,
        sch: str | Path,
        output_dir: str | Path,
        *,
        theme: str | None = None,
        black_and_white: bool = False,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess:
        """Export SVG from a .kicad_sch file."""
        args = ["sch", "export", "svg",
                "--output", str(output_dir),
                str(sch)]
        if theme:
            args += ["--theme", theme]
        if black_and_white:
            args.append("--black-and-white")
        return self.run(args, timeout=timeout)

    def sch_export_netlist(
        self,
        sch: str | Path,
        output_file: str | Path,
        *,
        format: str = "kicadxml",
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess:
        """Export netlist from a .kicad_sch file."""
        args = ["sch", "export", "netlist",
                "--output", str(output_file),
                "--format", format,
                str(sch)]
        return self.run(args, timeout=timeout)

    def sch_export_bom(
        self,
        sch: str | Path,
        output_file: str | Path,
        *,
        preset: str | None = None,
        format_preset: str | None = None,
        fields: str | None = None,
        labels: str | None = None,
        group_by: str | None = None,
        sort_field: str | None = None,
        sort_asc: bool = True,
        filter: str | None = None,
        exclude_dnp: bool = False,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess:
        """Export BOM from a .kicad_sch file."""
        args = ["sch", "export", "bom",
                "--output", str(output_file),
                str(sch)]
        if preset:
            args += ["--preset", preset]
        if format_preset:
            args += ["--format-preset", format_preset]
        if fields:
            args += ["--fields", fields]
        if labels:
            args += ["--labels", labels]
        if group_by:
            args += ["--group-by", group_by]
        if sort_field:
            args += ["--sort-field", sort_field]
        if not sort_asc:
            args.append("--sort-desc")
        if filter:
            args += ["--filter", filter]
        if exclude_dnp:
            args.append("--exclude-dnp")
        return self.run(args, timeout=timeout)

    def sch_export_pythonbom(
        self,
        sch: str | Path,
        output_file: str | Path,
        *,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess:
        """Export Python-script-style BOM from a .kicad_sch file."""
        args = ["sch", "export", "python-bom",
                "--output", str(output_file),
                str(sch)]
        return self.run(args, timeout=timeout)

    def sch_erc(
        self,
        sch: str | Path,
        output_file: str | Path,
        *,
        format: str = "json",
        units: str = "mm",
        severity_all: bool = False,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess:
        """Run ERC on a .kicad_sch file and write report."""
        args = ["sch", "erc",
                "--output", str(output_file),
                "--format", format,
                "--units", units,
                str(sch)]
        if severity_all:
            args.append("--severity-all")
        return self.run(args, timeout=timeout)
