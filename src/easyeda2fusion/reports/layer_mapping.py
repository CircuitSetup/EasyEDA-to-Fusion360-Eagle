from __future__ import annotations

from pathlib import Path

from easyeda2fusion.builders.layer_mapper import LayerMappingReport


def write_layer_mapping_report(report: LayerMappingReport, out_dir: Path) -> Path:
    path = out_dir / "layer_mapping_report.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.as_text() + "\n", encoding="utf-8")
    return path
