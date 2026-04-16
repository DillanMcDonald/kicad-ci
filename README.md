# kicad-ci

[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![KiCad 9](https://img.shields.io/badge/KiCad-9.0-brightgreen.svg)](https://www.kicad.org/)
[![GitHub Template](https://img.shields.io/badge/GitHub-Template-blueviolet.svg)](https://github.com/DillanMcDonald/kicad-ci/generate)

KiCad CI/CD pipeline — fully automated professional documentation on every push and PR.

---

## Quick install

### Option A — one-liner (existing repo)

Run from your KiCad project repo root:

```bash
curl -fsSL https://raw.githubusercontent.com/DillanMcDonald/kicad-ci/main/install.sh | bash
```

The script installs the workflow + scripts, auto-detects your project directory, and prints the one line you need to edit.

### Option B — GitHub template (new repo)

Click **[Use this template](https://github.com/DillanMcDonald/kicad-ci/generate)** on GitHub, then add your KiCad project files and set `PROJECT_DIR` in the workflow.

### Option C — manual copy

```bash
git clone https://github.com/DillanMcDonald/kicad-ci
cp -r kicad-ci/.github kicad-ci/scripts your-project/
```

---

## What it does

Every push and PR automatically runs:

| Job | Trigger | Output |
|-----|---------|--------|
| **ERC** | every push/PR | Electrical Rules Check — fails on errors, warns on warnings |
| **DRC** | parallel with ERC | Design Rules Check — fails on violations |
| **Export** | after DRC | Full professional documentation suite (see below) |
| **Release** | tag `v*` | GitHub Release with all docs attached |

### Documentation produced

```
output/
├── fab/
│   ├── gerbers/              One .gbr per copper/mask/silk/paste/edge layer
│   ├── drill/                Excellon drill + Gerber drill map
│   └── <name>-fab.zip        Ready-to-upload fab package (JLCPCB/PCBWay/OSHPark)
├── docs/
│   ├── schematic.pdf         Full schematic PDF
│   ├── schematic.svg         Schematic SVG
│   ├── assembly-front.pdf    Front assembly drawing (F.Cu + Fab + Silk + Courtyard)
│   ├── assembly-back.pdf     Back assembly drawing
│   └── board-all-layers.pdf  All copper + silk + mask + paste
├── preview/
│   ├── board-front.svg       Board top view — embed in README/wiki
│   └── board-back.svg        Board bottom view
├── assembly/
│   ├── bom.csv               Bill of Materials
│   ├── positions-front.csv   SMT pick-and-place / CPL (front)
│   ├── positions-back.csv    SMT pick-and-place / CPL (back)
│   └── <name>-assembly.zip   BOM + CPL ready for assembly house
└── 3d/
    ├── <name>.step           STEP model for MCAD / enclosure design
    ├── render-top.png        3D render top  (requires GPU runner, see below)
    └── render-bottom.png     3D render bottom
```

---

## Setup

### 1. Install

```bash
curl -fsSL https://raw.githubusercontent.com/DillanMcDonald/kicad-ci/main/install.sh | bash
```

### 2. Configure `PROJECT_DIR`

Open `.github/workflows/kicad-ci.yml` and set `PROJECT_DIR` to the directory
that contains your `.kicad_sch` and `.kicad_pcb` files.

```yaml
env:
  PROJECT_DIR: "hardware/my-board"   # path from repo root
```

If your KiCad files are at the repo root, **remove this line** — auto-discovery
will find them automatically.

### 3. Commit and push

```bash
git add .github/ scripts/ .gitattributes
git commit -m "ci: add kicad-ci pipeline"
git push
```

CI will run automatically. Check the **Actions** tab for results and artifacts.

### 4. Create a release

```bash
git tag v1.0.0
git push origin v1.0.0
```

A GitHub Release is created with all docs attached:
`fab.zip`, `docs.zip`, `assembly.zip`, `3d.zip`, `preview.zip`.

---

## Configuration

All config via env vars — set in `.github/workflows/kicad-ci.yml`:

| Variable | Default | Description |
|----------|---------|-------------|
| `PROJECT_DIR` | `.` | Directory containing `.kicad_sch` / `.kicad_pcb` |
| `SKIP_RENDER` | `0` | Set to `1` to skip 3D renders (GitHub-hosted runners have no GPU) |
| `KICAD_CLI` | `kicad-cli` | Override kicad-cli binary path |
| `OUTPUT_DIR` | `output` | Output root directory |

### 3D renders

3D renders (`render-top.png`, `render-bottom.png`) use KiCad's raytracer and
require an OpenGL-capable GPU. GitHub-hosted runners have no GPU, so renders
are skipped by default (`SKIP_RENDER=1` in the workflow).

To enable renders, run on a **self-hosted runner** with GPU:

```yaml
# In kicad-ci.yml, export job:
runs-on: [self-hosted, gpu]
env:
  SKIP_RENDER: "0"
```

### ERC behavior

The ERC job uses a two-pass strategy:
- **Pass 1** — full report (errors + warnings) written to artifact for engineer review
- **Pass 2** — errors-only exit-code check; CI fails only on actual ERC errors

Intentional unconnected pins (common on MCU/SoC carrier boards) are ERC
*warnings*, not errors — they do not fail CI.

If your board is an upstream fork with known ERC violations, add
`continue-on-error: true` to the ERC step.

---

## Library dependencies

Custom KiCad libraries (symbols, footprints) must be in the repo and referenced
via relative paths using `${KIPRJMOD}` in your `sym-lib-table` and `fp-lib-table`.

The `${KIPRJMOD}` variable resolves to the directory of the `.kicad_pro` file.
Paths are relative to that directory:

```
# Correct — library is one level up from the project file
${KIPRJMOD}/../libraries/MyLib/MyLib.kicad_sym

# Also correct — library is in the same directory as the project file
${KIPRJMOD}/MyLib/MyLib.kicad_sym
```

---

## License audit

| Tool | License | Role |
|------|---------|------|
| `kicad-cli` (KiCad 9) | GPL-3.0 | External binary — caller scripts unaffected |
| `ghcr.io/kicad/kicad:9.0` | GPL-3.0 Dockerfile | CI container — using ≠ distributing |
| `actions/checkout@v4` | MIT | Source checkout |
| `actions/upload-artifact@v4` | MIT | Artifact storage |
| `actions/download-artifact@v4` | MIT | Artifact retrieval |
| `gh` CLI | MIT | Release creation |
| All `scripts/*.sh` (this repo) | **MIT** | All pipeline logic |

Not used (license incompatible with MIT-only constraint):
- KiBot — AGPL-3.0
- Interactive HTML BOM plugin — GPL-3.0

---

## Running locally

Requires KiCad 8+ installed (provides `kicad-cli`).

```bash
# ERC
bash scripts/erc.sh

# DRC
bash scripts/drc.sh

# Export everything
bash scripts/export.sh

# Custom project dir
PROJECT_DIR=hardware/my-board bash scripts/export.sh

# Skip 3D render
SKIP_RENDER=1 bash scripts/export.sh
```

---

## License

MIT — see [LICENSE](LICENSE).

KiCad itself is GPL-3.0. Running it as an external CLI tool in CI does not
affect the license of your pipeline scripts or design files.
