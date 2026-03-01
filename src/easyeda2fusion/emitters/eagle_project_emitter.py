from __future__ import annotations

from pathlib import Path
from xml.dom import minidom
import xml.etree.ElementTree as ET

from easyeda2fusion.model import Project


def emit_project_artifacts(project: Project, out_dir: Path) -> dict[str, Path]:
    artifacts: dict[str, Path] = {}
    out_dir.mkdir(parents=True, exist_ok=True)
    project_name = _safe_name(project.name)

    epf_path = out_dir / "eagle.epf"
    epf_path.write_text('[Eagle]\nVersion="09 06 02"\n', encoding="utf-8")
    artifacts["project_file"] = epf_path

    schematic_path = out_dir / f"{project_name}.sch"
    _emit_schematic_scaffold(project, schematic_path)
    artifacts["schematic_file"] = schematic_path

    board_path = out_dir / f"{project_name}.brd"
    _emit_board_scaffold(project, board_path)
    artifacts["board_file"] = board_path

    project_stub = out_dir / f"{project_name}.eagle_project.txt"
    with project_stub.open("w", encoding="utf-8") as f:
        f.write("This converter emits conservative EAGLE scaffold files and reconstruction scripts.\n")
        f.write("Use scripts/rebuild_project.scr to reconstruct full schematic and board intent.\n")
        f.write(f"Project: {project.name}\n")
        f.write(f"Source format: {project.source_format.value}\n")
        f.write(f"Schematic scaffold: {schematic_path.name}\n")
        f.write(f"Board scaffold: {board_path.name}\n")
        f.write(f"Project file: {epf_path.name}\n")
    artifacts["project_stub"] = project_stub

    lib_stub = out_dir / "generated_library.lbr.txt"
    with lib_stub.open("w", encoding="utf-8") as f:
        f.write("Generated/Matched library summary\n")
        f.write("Use accompanying JSON manifests and .scr scripts to reconstruct in EAGLE/Fusion.\n")
        for device in project.devices:
            f.write(f"DEVICE {device.device_id} package={device.package_id} symbol={device.symbol_id}\n")
    artifacts["library_stub"] = lib_stub

    return artifacts


def _emit_schematic_scaffold(project: Project, output_path: Path) -> None:
    root = ET.Element("eagle", {"version": "9.6.2"})
    drawing = ET.SubElement(root, "drawing")
    _add_common_header(drawing, grid_distance="0.1")

    schematic = ET.SubElement(
        drawing,
        "schematic",
        {"xreflabel": "%F%N/%S.%C%R", "xrefpart": "/%S.%C%R"},
    )
    ET.SubElement(schematic, "libraries")
    ET.SubElement(schematic, "attributes")
    ET.SubElement(schematic, "variantdefs")

    classes = ET.SubElement(schematic, "classes")
    ET.SubElement(classes, "class", {"number": "0", "name": "default", "width": "0", "drill": "0"})

    ET.SubElement(schematic, "parts")

    sheets = ET.SubElement(schematic, "sheets")
    sheet = ET.SubElement(sheets, "sheet")
    plain = ET.SubElement(sheet, "plain")
    ET.SubElement(
        plain,
        "text",
        {"x": "0", "y": "0", "size": "1.27", "layer": "94"},
    ).text = (
        "Generated schematic scaffold. Use scripts/rebuild_schematic.scr for full reconstruction."
    )
    ET.SubElement(
        plain,
        "text",
        {"x": "0", "y": "-2.54", "size": "1.27", "layer": "94"},
    ).text = f"Components={len(project.components)} Nets={len(project.nets)} Sheets={len(project.sheets)}"

    ET.SubElement(sheet, "instances")
    ET.SubElement(sheet, "busses")
    ET.SubElement(sheet, "nets")

    _write_pretty_xml(output_path, root)


def _emit_board_scaffold(project: Project, output_path: Path) -> None:
    root = ET.Element("eagle", {"version": "9.6.2"})
    drawing = ET.SubElement(root, "drawing")
    _add_common_header(drawing, grid_distance="0.05")

    board = ET.SubElement(drawing, "board")
    plain = ET.SubElement(board, "plain")

    if project.board is not None:
        for region in project.board.outline:
            points = region.points
            if len(points) < 2:
                continue
            for idx in range(len(points)):
                start = points[idx]
                end = points[(idx + 1) % len(points)]
                ET.SubElement(
                    plain,
                    "wire",
                    {
                        "x1": _fmt_mm(start.x_mm),
                        "y1": _fmt_mm(start.y_mm),
                        "x2": _fmt_mm(end.x_mm),
                        "y2": _fmt_mm(end.y_mm),
                        "width": "0",
                        "layer": "20",
                    },
                )

    ET.SubElement(
        plain,
        "text",
        {"x": "0", "y": "0", "size": "1.27", "layer": "21"},
    ).text = "Generated board scaffold. Use scripts/rebuild_board.scr for full reconstruction."
    ET.SubElement(
        plain,
        "text",
        {"x": "0", "y": "-2.54", "size": "1.27", "layer": "21"},
    ).text = (
        f"Tracks={len(project.board.tracks) if project.board else 0} "
        f"Vias={len(project.board.vias) if project.board else 0} "
        f"Pads={len(project.board.pads) if project.board else 0}"
    )

    ET.SubElement(board, "libraries")
    ET.SubElement(board, "attributes")
    ET.SubElement(board, "variantdefs")

    classes = ET.SubElement(board, "classes")
    ET.SubElement(classes, "class", {"number": "0", "name": "default", "width": "0", "drill": "0"})

    ET.SubElement(board, "designrules", {"name": "default"})
    autorouter = ET.SubElement(board, "autorouter")
    ET.SubElement(autorouter, "pass", {"name": "Default", "refer": "Default", "active": "yes"})
    ET.SubElement(board, "elements")
    ET.SubElement(board, "signals")

    _write_pretty_xml(output_path, root)


def _add_common_header(drawing: ET.Element, grid_distance: str) -> None:
    settings = ET.SubElement(drawing, "settings")
    ET.SubElement(settings, "setting", {"alwaysvectorfont": "no"})
    ET.SubElement(settings, "setting", {"verticaltext": "up"})

    ET.SubElement(
        drawing,
        "grid",
        {
            "distance": grid_distance,
            "unitdist": "mm",
            "unit": "mm",
            "style": "lines",
            "multiple": "1",
            "display": "no",
            "altdistance": "0.01",
            "altunitdist": "mm",
            "altunit": "mm",
        },
    )

    layers = ET.SubElement(drawing, "layers")
    for number, name, color, fill in [
        (1, "Top", 4, 1),
        (16, "Bottom", 1, 1),
        (17, "Pads", 2, 1),
        (18, "Vias", 2, 1),
        (19, "Unrouted", 6, 1),
        (20, "Dimension", 15, 1),
        (21, "tPlace", 7, 1),
        (22, "bPlace", 7, 1),
        (25, "tNames", 7, 1),
        (26, "bNames", 7, 1),
        (27, "tValues", 7, 1),
        (28, "bValues", 7, 1),
        (29, "tStop", 7, 3),
        (30, "bStop", 7, 6),
        (31, "tCream", 7, 4),
        (32, "bCream", 7, 5),
        (39, "tKeepout", 4, 11),
        (40, "bKeepout", 1, 11),
        (41, "tRestrict", 4, 10),
        (42, "bRestrict", 1, 10),
        (44, "Drills", 7, 1),
        (45, "Holes", 7, 1),
        (46, "Milling", 3, 1),
        (51, "tDocu", 7, 1),
        (52, "bDocu", 7, 1),
        (91, "Nets", 2, 1),
        (94, "Symbols", 4, 1),
        (95, "Names", 7, 1),
        (96, "Values", 7, 1),
    ]:
        ET.SubElement(
            layers,
            "layer",
            {
                "number": str(number),
                "name": name,
                "color": str(color),
                "fill": str(fill),
                "visible": "yes",
                "active": "yes",
            },
        )


def _write_pretty_xml(output_path: Path, root: ET.Element) -> None:
    xml_bytes = ET.tostring(root, encoding="utf-8")
    pretty = minidom.parseString(xml_bytes).toprettyxml(indent="  ", encoding="utf-8")
    output_path.write_bytes(pretty)


def _safe_name(value: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in {"_", "-", " "} else "_" for ch in str(value or "project"))
    text = text.strip().replace(" ", "_")
    return text or "project"


def _fmt_mm(value: float) -> str:
    return f"{float(value):.4f}".rstrip("0").rstrip(".") or "0"
