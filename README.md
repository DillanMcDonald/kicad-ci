# kicad-ci

KiCad CI/CD pipeline via GitHub Actions. Runs on every push and PR.

## What it does

| Job | Trigger | What happens |
|-----|---------|-------------|
| **ERC** | push / PR | Electrical Rules Check — fails on any violation |
| **DRC** | after ERC passes | Design Rules Check + schematic-PCB net parity — fails on any violation |
| **Export** | after both pass | Gerbers, drill (Excellon), schematic PDF, BOM CSV, 3D render PNGs, fab ZIP |
| **Release** | tag `v*` | GitHub Release created with fab ZIP, PDF, BOM, renders attached |

## License audit

| Tool | License | Role | OK? |
|------|---------|------|-----|
| `kicad-cli` (KiCad 9) | GPL-3.0 | External binary called by scripts | ✅ Running GPL binary does not infect caller scripts |
| `ghcr.io/kicad/kicad:9.0` | GPL-3.0 (Dockerfile) | CI container image | ✅ Using ≠ redistributing |
| `actions/checkout@v4` | MIT | Source checkout | ✅ |
| `actions/upload-artifact@v4` | MIT | Artifact storage | ✅ |
| `actions/download-artifact@v4` | MIT | Artifact retrieval | ✅ |
| `gh` CLI (GitHub CLI) | MIT | Release creation | ✅ |
| All `scripts/*.sh` | MIT | Pipeline logic (this repo) | ✅ |

**Not used (license incompatible):**
- KiBot — AGPL-3.0 ❌
- Interactive HTML BOM plugin — GPL-3.0 ❌

## Usage

### Add to an existing KiCad project repo

```bash
# Copy the CI config into your project
cp -r path/to/kicad-ci/.github your-kicad-project/
cp -r path/to/kicad-ci/scripts your-kicad-project/
```

Or use this repo as a template (GitHub → "Use this template").

Your project structure should look like:

```
your-project/
├── your-board.kicad_pro
├── your-board.kicad_sch
├── your-board.kicad_pcb
├── scripts/          ← from this repo
└── .github/
    └── workflows/
        └── kicad-ci.yml
```

Scripts auto-discover `*.kicad_sch` and `*.kicad_pcb` (up to 4 dirs deep).

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PROJECT_DIR` | `.` | Root dir to search for KiCad files |
| `OUTPUT_DIR` | `output` | Where artifacts are written |
| `KICAD_CLI` | `kicad-cli` | Path to kicad-cli binary |
| `SKIP_RENDER` | `0` | Set to `1` to skip 3D render |

Override in the workflow `env:` block if your project files are in a subdirectory:

```yaml
env:
  PROJECT_DIR: hardware/my-board
```

### Run locally

Requires KiCad 8+ installed (provides `kicad-cli`).

```bash
# ERC
bash scripts/erc.sh

# DRC
bash scripts/drc.sh

# Export everything
bash scripts/export.sh

# Skip 3D render
SKIP_RENDER=1 bash scripts/export.sh
```

### Tagging a release

```bash
git tag v1.0.0
git push origin v1.0.0
```

GitHub Actions will run the full pipeline, then create a release at
`https://github.com/YOUR_ORG/YOUR_REPO/releases/tag/v1.0.0` with:
- `*-fab.zip` — Gerbers + drill files ready for JLCPCB / PCBWay / OSHPark
- `schematic.pdf`
- `bom.csv`
- `render-top.png` / `render-bottom.png`

## Artifacts

All artifacts are uploaded as GitHub Actions artifacts on every run.  
Fab ZIP is also attached to GitHub Releases on version tags.

```
output/
├── gerbers/          # one .gbr per layer
├── drill/            # Excellon drill files
├── <name>-fab.zip    # gerbers/ + drill/ zipped for fab upload
├── schematic.pdf
├── bom.csv
├── render-top.png
└── render-bottom.png
```

## License

MIT — see [LICENSE](LICENSE).

KiCad itself is GPL-3.0. Running it as an external tool in CI does not affect
the license of your pipeline scripts or design files.
