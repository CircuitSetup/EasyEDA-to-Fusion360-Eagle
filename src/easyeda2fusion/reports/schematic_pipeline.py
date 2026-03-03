from __future__ import annotations

from pathlib import Path
from typing import Any

from easyeda2fusion.model import Project
from easyeda2fusion.utils.io import dump_json


def write_schematic_pipeline_reports(project: Project, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    reports: dict[str, Path] = {}

    payloads: list[tuple[str, str, Any]] = [
        ("symbol_geometry_map", "schematic_symbol_geometry_map", project.metadata.get("schematic_symbol_geometry_map")),
        ("symbol_origin_map", "schematic_symbol_origin_map", project.metadata.get("schematic_symbol_origin_map")),
        ("board_net_connection_map", "schematic_board_net_connection_map", project.metadata.get("schematic_board_net_connection_map")),
        ("board_placement_map", "schematic_board_placement_map", project.metadata.get("schematic_board_placement_map")),
        ("net_attachment_plan", "schematic_net_attachment_plan", project.metadata.get("schematic_net_attachment_plan")),
        ("pipeline_validation_summary", "schematic_pipeline_validation_summary", project.metadata.get("schematic_pipeline_validation_summary")),
        ("pin_anchor_diagnostics", "schematic_pin_anchor_diagnostics", project.metadata.get("schematic_pin_anchor_diagnostics")),
    ]

    for short_name, file_name, payload in payloads:
        if payload is None:
            continue
        json_path = out_dir / f"{file_name}.json"
        text_path = out_dir / f"{file_name}.txt"
        dump_json(json_path, payload)
        text_path.write_text(_payload_to_text(short_name, payload), encoding="utf-8")
        reports[f"{short_name}_json"] = json_path
        reports[f"{short_name}_text"] = text_path

    return reports


def _payload_to_text(name: str, payload: Any) -> str:
    lines = [f"Schematic Pipeline Report: {name}", ""]
    if isinstance(payload, dict):
        for key in sorted(payload.keys()):
            value = payload[key]
            if isinstance(value, (dict, list)):
                lines.append(f"{key}: <{type(value).__name__}>")
            else:
                lines.append(f"{key}: {value}")
    elif isinstance(payload, list):
        lines.append(f"items: {len(payload)}")
    else:
        lines.append(str(payload))
    lines.append("")
    return "\n".join(lines)
