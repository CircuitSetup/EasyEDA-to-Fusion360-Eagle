# EasyEDA to Fusion 360 Electronics / EAGLE Converter

Python-first converter for importing EasyEDA Standard/Lite and EasyEDA Pro schematic/PCB data, normalizing into a shared model, and emitting Fusion 360 Electronics/EAGLE reconstruction artifacts.

The tool is intentionally conservative:
- prioritizes correctness and traceability
- emits explicit warnings for ambiguity/loss
- generates rebuild scripts and manifests instead of pretending perfect native file synthesis

## Features

- Separate parsers:
  - `easyeda_std.py` for EasyEDA Standard/Lite
  - `easyeda_pro.py` for EasyEDA Pro (including local project bundles with `project.json` + `SHEET/*.esch` + `PCB/*.epcb`)
- Shared normalized internal model for:
  - project, sheets, components, symbols, packages, devices, nets
  - board, tracks, vias, pads, arcs, regions/polygons/pours
  - mechanical/text/layers/rules/metadata
- Explicit layer mapping system with lossy-mapping warnings and report
- Staged part matching pipeline:
  1. exact MPN/device/package matching
  2. non-passive part-number matching against Fusion/EAGLE library metadata (MPN/device aliases)
  3. package + component-class fallback
     - resistor/capacitor package-size matching prefers installed Fusion/EAGLE library devices (for example `rcl` `0603/0805`) before generating local parts
  4. ambiguity handling (prompt mode supported)
  5. conservative library synthesis (when evidence is sufficient)
  6. unresolved parts report
- Board-only inferred schematic reconstruction with explicit uncertainty reporting
- Validation/report generation for manual review workflows
- CLI and optional simple Tkinter picker flow
- Optional minimal ULP launcher shim (`ulp/easyeda2fusion_launcher.ulp`)
  - probes for Python, can attempt `winget` Python install, checks `easyeda2fusion` module presence, offers `pip install -e` when missing
  - can optionally pass:
    - a general library folder/file for matching
    - a preferred resistor library path
    - a preferred capacitor library path
  - launches the CLI command in terminal with selected paths

## Installation

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e .
pip install -e .[dev]
```

## Usage

### CLI

```bash
easyeda2fusion \
  --input C:\path\design.json \
  --output C:\path\converted \
  --mode full \
  --match-mode auto
```

### Optional GUI picker

```bash
easyeda2fusion --gui
```

### Conversion Modes

- `full`: schematic + board conversion
- `schematic`: schematic only
- `board`: board only
- `board-infer-schematic`: board plus conservative inferred schematic reconstruction

### Library Matching Modes

- `auto`: staged automatic matching and conservative synthesis
- `prompt`: interactive ambiguity resolution
- `strict`: exact-only matching, no fallback
- `package-first`: package/class fallback enabled (same engine, fallback-prioritized behavior)

### Optional Arguments

- `--source-format easyeda_std|easyeda_pro` to force parser family
- `--library <path>` JSON file/folder with target library entries
  - supports JSON index files and `.lbr` files/folders for matching to existing Fusion/EAGLE libraries
  - if omitted, the converter auto-scans common EAGLE/Fusion library locations, including:
    - `%LOCALAPPDATA%\\Autodesk\\Autodesk Fusion 360\\Electron\\lbr`
- `--resistor-library <path>` preferred resistor `.lbr`/JSON file or folder
- `--capacitor-library <path>` preferred capacitor `.lbr`/JSON file or folder
- `--verbose` for debug logs

## Output Structure

Example output directory:

```text
converted/
  conversion.log
  summary.json
  summary.txt
  artifacts/
    eagle.epf
    <project>.sch
    <project>.brd
    <project>.eagle_project.txt
    generated_library.lbr.txt
  scripts/
    rebuild_schematic.scr
    rebuild_board.scr
    rebuild_project.scr
  manifests/
    source_manifest.json
    normalized_project.json
    inferred_schematic_manifest.json   # present when inference runs
  library/
    easyeda_generated.lbr                # auto-generated fallback library for created devices
    library_manifest.json
    library_manifest.txt
  reports/
    layer_mapping_report.txt
    validation_report.json
    validation_report.txt
    unresolved_parts.csv
    unresolved_parts.json
```

## Reports

The converter emits both machine-readable and human-readable reports for:
- layer mapping (including lossy mapping flags)
- unresolved parts
- validation checks and manual review items
- summary metrics:
  - detected source type
  - schematic converted/inferred/skipped
  - board converted/skipped
  - auto-matched parts
  - user-input-needed parts
  - new library parts created
  - unresolved parts
  - major warnings

## Manual Recovery Workflow

1. Inspect `reports/validation_report.txt` and `reports/unresolved_parts.csv`.
2. Resolve ambiguous/unresolved library mappings (import/create missing parts in target libraries).
3. Re-run conversion with:
   - `--match-mode prompt` for interactive choices, or
   - `--library <custom index>` after adding mappings.
4. Import/replay generated `.scr` scripts in Fusion/EAGLE.
5. Cross-check inferred or lossy items listed in manifests/reports.

## Known Limitations

- EasyEDA exports differ by version; unsupported objects are preserved with warnings instead of silently dropped.
- Direct native `.sch/.brd/.lbr` generation is intentionally limited; script-driven reconstruction is preferred for reliability.
- Datasheet-driven package synthesis is conservative and will stop when geometry evidence is insufficient.
- Board-only schematic inference is best-effort and explicitly marked as inferred/ambiguous.

## Testing

Run:

```bash
pytest
```

The test suite covers:
- Standard/Lite schematic import
- Pro schematic import
- Standard/Lite board import
- Pro board import
- full conversion pipeline
- board-only inferred schematic reconstruction
- ambiguous part matching
- missing library generation
- unresolved part reporting
- multilayer board layer mapping
