from __future__ import annotations

import copy
import logging
import math
import re
from dataclasses import dataclass
from typing import Any

from easyeda2fusion.builders.layer_mapper import LayerMappingReport, map_layers
from easyeda2fusion.model import (
    Arc,
    Board,
    Component,
    Hole,
    Net,
    NetNode,
    Package,
    Pad,
    ParsedDocument,
    ParsedSource,
    Point,
    Project,
    Region,
    Rule,
    SchematicSheet,
    Severity,
    Side,
    SourceFormat,
    Symbol,
    SymbolPin,
    TextItem,
    Track,
    Via,
    project_event,
)
from easyeda2fusion.utils.units import UnitNormalizer

log = logging.getLogger(__name__)


@dataclass
class NormalizationResult:
    project: Project
    layer_report: LayerMappingReport


class Normalizer:
    def normalize(self, parsed: ParsedSource) -> NormalizationResult:
        unit_norm = UnitNormalizer(UnitNormalizer.from_metadata(parsed.source_format, parsed.metadata))
        mapped_layers, layer_report, layer_events = map_layers(parsed.source_format, parsed.layers)

        project = Project(
            project_id=_slug(parsed.metadata.get("project_id") or parsed.metadata.get("uuid") or "project"),
            name=str(parsed.metadata.get("name") or parsed.metadata.get("projectName") or "easyeda_project"),
            source_format=parsed.source_format,
            input_files=parsed.input_files,
            layers=mapped_layers,
            rules=[
                Rule(
                    name=str(rule.get("name", "unnamed_rule")),
                    value=str(rule.get("value", "")),
                    description=str(rule.get("description")) if rule.get("description") is not None else None,
                )
                for rule in parsed.rules
            ],
            metadata={**parsed.metadata},
            events=[*parsed.events, *layer_events],
        )

        raw_packages = parsed.metadata.get("footprint_packages")
        if isinstance(raw_packages, list):
            for raw_package in raw_packages:
                if not isinstance(raw_package, dict):
                    continue
                package = self._package_from_obj(raw_package, unit_norm)
                _upsert_package(project.packages, package)

        for document in parsed.documents:
            if document.doc_type == "schematic":
                self._normalize_schematic_doc(project, document, unit_norm)
            elif document.doc_type == "board":
                self._normalize_board_doc(project, document, unit_norm)
            else:
                project.events.append(
                    project_event(
                        Severity.WARNING,
                        "UNKNOWN_DOCUMENT_TYPE",
                        f"Unknown document type '{document.doc_type}' skipped",
                        {"document": document.name},
                    )
                )

        self._harmonize_legacy_package_local_frames(project)
        self._attach_default_board_layers(project)
        return NormalizationResult(project=project, layer_report=layer_report)

    def _normalize_schematic_doc(
        self,
        project: Project,
        document: ParsedDocument,
        unit_norm: UnitNormalizer,
    ) -> None:
        sheet = SchematicSheet(
            sheet_id=_slug(document.metadata.get("id") or document.name),
            name=document.name,
        )

        for obj in document.raw_objects:
            typ = _obj_type(obj)

            if typ in {"component", "part", "symbol_instance", "sch_component"}:
                component = self._component_from_obj(obj, unit_norm, sheet.sheet_id)
                _upsert_component(project.components, component)
                sheet.components.append(component.refdes)
            elif typ in {"symbol", "symbol_def", "symbol_definition"}:
                symbol = self._symbol_from_obj(obj, unit_norm)
                _upsert_symbol(project.symbols, symbol)
            elif typ in {"net", "netlabel", "wirenet"}:
                net = self._net_from_obj(obj)
                _upsert_net(project.nets, net)
            elif typ in {"wire", "line", "polyline"}:
                wire = _normalize_wire(obj, unit_norm)
                sheet.wires.append(wire)
            elif typ in {"label", "text", "annotation", "net_label"}:
                sheet.annotations.append(self._text_from_obj(obj, unit_norm, layer="schematic_text"))
            elif typ in {"port", "power_port", "sheet_port"}:
                sheet.ports.append(obj)
            elif typ in {"junction", "dot"}:
                x, y = _point_from_obj(obj, unit_norm)
                sheet.junctions.append(Point(x, y))
            elif typ in {"no_connect", "nc"}:
                x, y = _point_from_obj(obj, unit_norm)
                sheet.no_connects.append(Point(x, y))
            elif typ in {"device", "device_def"}:
                # Stored in metadata for traceability if explicit device object appears.
                project.metadata.setdefault("source_devices", []).append(obj)
            elif typ in {"package", "footprint", "package_def"}:
                pkg = self._package_from_obj(obj, unit_norm)
                _upsert_package(project.packages, pkg)
            else:
                project.events.append(
                    project_event(
                        Severity.WARNING,
                        "UNSUPPORTED_SCHEMATIC_OBJECT",
                        f"Unsupported schematic object '{typ}' kept in metadata for review",
                        {"sheet": document.name, "type": typ},
                    )
                )
                project.metadata.setdefault("unsupported_schematic_objects", []).append(obj)

        project.sheets.append(sheet)

    def _normalize_board_doc(
        self,
        project: Project,
        document: ParsedDocument,
        unit_norm: UnitNormalizer,
    ) -> None:
        if project.board is None:
            project.board = Board(layers=list(project.layers))
        board = project.board

        for obj in document.raw_objects:
            typ = _obj_type(obj)

            if typ in {"track", "segment"}:
                board.tracks.append(self._track_from_obj(obj, unit_norm))
            elif typ in {"via"}:
                board.vias.append(self._via_from_obj(obj, unit_norm))
            elif typ in {"pad"}:
                board.pads.append(self._pad_from_obj(obj, unit_norm))
            elif typ in {"arc"}:
                board.arcs.append(self._arc_from_obj(obj, unit_norm))
            elif typ in {"polygon", "pour", "region"}:
                board.regions.append(self._region_from_obj(obj, unit_norm, kind="polygon"))
            elif typ in {"keepout", "restrict"}:
                board.keepouts.append(self._region_from_obj(obj, unit_norm, kind="keepout"))
            elif typ in {"outline", "board_outline", "dimension"}:
                board.outline.append(self._region_from_obj(obj, unit_norm, kind="outline"))
            elif typ in {"cutout", "slot"}:
                board.cutouts.append(self._region_from_obj(obj, unit_norm, kind="cutout"))
            elif typ in {"hole", "drill"}:
                board.holes.append(self._hole_from_obj(obj, unit_norm))
            elif typ in {"text", "label"}:
                board.text.append(self._text_from_obj(obj, unit_norm, layer=str(obj.get("layer", "documentation"))))
            elif typ in {"component", "footprint_instance", "placement"}:
                component = self._component_from_obj(obj, unit_norm, sheet_id=None)
                _upsert_component(project.components, component)
            elif typ in {"package", "footprint", "package_def"}:
                pkg = self._package_from_obj(obj, unit_norm)
                _upsert_package(project.packages, pkg)
            elif typ in {"net", "board_net"}:
                _upsert_net(project.nets, self._net_from_obj(obj))
            elif typ in {"mechanical", "fab_note"}:
                board.mechanical.append(obj)
            else:
                project.events.append(
                    project_event(
                        Severity.WARNING,
                        "UNSUPPORTED_BOARD_OBJECT",
                        f"Unsupported board object '{typ}' kept in metadata for review",
                        {"board": document.name, "type": typ},
                    )
                )
                project.metadata.setdefault("unsupported_board_objects", []).append(obj)

        self._ensure_board_outline(project, board)

    @staticmethod
    def _component_from_obj(obj: dict[str, Any], unit_norm: UnitNormalizer, sheet_id: str | None) -> Component:
        refdes = str(
            obj.get("refdes")
            or obj.get("designator")
            or obj.get("ref")
            or obj.get("name")
            or "UNNAMED"
        )
        value = str(obj.get("value", ""))
        source_name = str(obj.get("part_name") or obj.get("source_name") or obj.get("name") or refdes)
        x, y = _point_from_obj(obj, unit_norm)

        side_raw = str(obj.get("side", obj.get("layer_side", "top"))).lower()
        side = Side.TOP if side_raw in {"top", "t"} else Side.BOTTOM if side_raw in {"bottom", "b"} else Side.UNKNOWN

        component = Component(
            refdes=refdes,
            value=value,
            source_name=source_name,
            source_instance_id=_empty_to_none(obj.get("id") or obj.get("source_instance_id")),
            symbol_id=_empty_to_none(obj.get("symbol_id") or obj.get("symbol")),
            package_id=_empty_to_none(
                obj.get("package_id")
                or obj.get("package")
                or obj.get("footprint")
                or (obj.get("attributes", {}).get("Footprint") if isinstance(obj.get("attributes"), dict) else None)
                or (obj.get("attributes", {}).get("Package") if isinstance(obj.get("attributes"), dict) else None)
            ),
            device_id=_empty_to_none(obj.get("device_id") or obj.get("device")),
            manufacturer=_empty_to_none(obj.get("manufacturer")),
            mpn=_empty_to_none(obj.get("mpn") or obj.get("part_number")),
            attributes=dict(obj.get("attributes", {})) if isinstance(obj.get("attributes"), dict) else {},
            at=Point(x, y),
            rotation_deg=float(obj.get("rotation", obj.get("rot", 0.0)) or 0.0),
            side=side,
            sheet_id=sheet_id,
        )

        for key in ("comment", "description", "package_name", "datasheet"):
            if key in obj and obj[key] is not None:
                component.attributes[key] = obj[key]

        return component

    @staticmethod
    def _symbol_from_obj(obj: dict[str, Any], unit_norm: UnitNormalizer) -> Symbol:
        symbol_id = str(obj.get("id") or obj.get("symbol_id") or obj.get("name") or "symbol")
        name = str(obj.get("name") or symbol_id)
        pins: list[SymbolPin] = []
        graphics: list[dict[str, Any]] = []

        raw_pins = obj.get("pins")
        if isinstance(raw_pins, list):
            for pin in raw_pins:
                if not isinstance(pin, dict):
                    continue
                x, y = _point_from_obj(pin, unit_norm)
                rotation_raw = pin.get("rotation")
                length_raw = pin.get("length")
                rotation_deg: float | None = None
                length_mm: float | None = None
                if rotation_raw is not None:
                    try:
                        rotation_deg = float(rotation_raw)
                    except Exception:
                        rotation_deg = None
                if length_raw is not None:
                    try:
                        length_mm = unit_norm.scalar_to_mm(float(length_raw))
                    except Exception:
                        length_mm = None
                pins.append(
                    SymbolPin(
                        pin_number=str(pin.get("number", pin.get("pin", ""))),
                        pin_name=str(pin.get("name", "")),
                        at=Point(x, y),
                        rotation_deg=rotation_deg,
                        length_mm=length_mm,
                    )
                )

        origin_x_raw = obj.get("origin_x", obj.get("originX"))
        origin_y_raw = obj.get("origin_y", obj.get("originY"))
        if origin_x_raw is not None and origin_y_raw is not None:
            try:
                origin_x_mm = unit_norm.scalar_to_mm(float(origin_x_raw))
                origin_y_mm = unit_norm.scalar_to_mm(float(origin_y_raw))
                graphics.append(
                    {
                        "kind": "origin",
                        "x_mm": origin_x_mm,
                        "y_mm": origin_y_mm,
                    }
                )
            except Exception:
                pass

        return Symbol(symbol_id=symbol_id, name=name, pins=pins, graphics=graphics)

    @staticmethod
    def _net_from_obj(obj: dict[str, Any]) -> Net:
        name = str(obj.get("name") or obj.get("net") or obj.get("net_name") or "N$UNNAMED").strip()
        if not name:
            name = "N$UNNAMED"
        nodes: list[NetNode] = []
        raw_nodes = obj.get("nodes")
        if isinstance(raw_nodes, list):
            for node in raw_nodes:
                if not isinstance(node, dict):
                    continue
                nodes.append(
                    NetNode(
                        refdes=str(node.get("refdes") or node.get("ref") or "").strip(),
                        pin=str(node.get("pin") or node.get("pin_number") or "").strip(),
                    )
                )
        return Net(name=name, nodes=nodes)

    @staticmethod
    def _track_from_obj(obj: dict[str, Any], unit_norm: UnitNormalizer) -> Track:
        sx, sy = _point_pair(obj, unit_norm, start_keys=("x1", "y1"), fallback=("sx", "sy"))
        ex, ey = _point_pair(obj, unit_norm, start_keys=("x2", "y2"), fallback=("ex", "ey"))
        return Track(
            start=Point(sx, sy),
            end=Point(ex, ey),
            width_mm=unit_norm.scalar_to_mm(float(obj.get("width", 0.2))),
            layer=str(obj.get("layer", "top_copper")),
            net=_empty_to_none(obj.get("net") or obj.get("net_name")),
        )

    @staticmethod
    def _via_from_obj(obj: dict[str, Any], unit_norm: UnitNormalizer) -> Via:
        x, y = _point_from_obj(obj, unit_norm)
        return Via(
            at=Point(x, y),
            drill_mm=unit_norm.scalar_to_mm(float(obj.get("drill", obj.get("drill_diameter", 0.3)))),
            diameter_mm=unit_norm.scalar_to_mm(float(obj.get("diameter", obj.get("size", 0.6)))),
            net=_empty_to_none(obj.get("net") or obj.get("net_name")),
            start_layer=str(obj.get("start_layer", "top_copper")),
            end_layer=str(obj.get("end_layer", "bottom_copper")),
        )

    @staticmethod
    def _pad_from_obj(obj: dict[str, Any], unit_norm: UnitNormalizer) -> Pad:
        x, y = _point_from_obj(obj, unit_norm)
        return Pad(
            pad_number=str(obj.get("number") or obj.get("pad") or obj.get("name") or ""),
            at=Point(x, y),
            shape=str(obj.get("shape", "rect")),
            width_mm=unit_norm.scalar_to_mm(float(obj.get("width", obj.get("w", 1.0)))),
            height_mm=unit_norm.scalar_to_mm(float(obj.get("height", obj.get("h", 1.0)))),
            drill_mm=unit_norm.scalar_to_mm(float(obj["drill"])) if obj.get("drill") is not None else None,
            layer=str(obj.get("layer", "top_copper")),
            rotation_deg=float(obj.get("rotation", obj.get("rot", 0.0)) or 0.0),
            net=_empty_to_none(obj.get("net") or obj.get("net_name")),
            component_refdes=_empty_to_none(obj.get("component_refdes") or obj.get("refdes")),
            source_instance_id=_empty_to_none(obj.get("source_instance_id")),
        )

    @staticmethod
    def _package_from_obj(obj: dict[str, Any], unit_norm: UnitNormalizer) -> Package:
        package_id = str(obj.get("id") or obj.get("package_id") or obj.get("name") or "package")
        package_name = str(obj.get("name") or package_id)
        pads = []
        raw_pads = obj.get("pads")
        if isinstance(raw_pads, list):
            for raw_pad in raw_pads:
                if not isinstance(raw_pad, dict):
                    continue
                pads.append(Normalizer._package_pad_from_obj(raw_pad, unit_norm))

        outline: list[dict[str, Any]] = []
        raw_outline = obj.get("outline")
        if isinstance(raw_outline, list):
            for item in raw_outline:
                if not isinstance(item, dict):
                    continue
                kind = str(item.get("kind", "")).strip().lower()
                layer = str(item.get("layer", "")).strip()
                if kind == "wire_path":
                    points: list[dict[str, float]] = []
                    raw_points = item.get("points")
                    if isinstance(raw_points, list):
                        for point in raw_points:
                            if isinstance(point, dict):
                                if "x_mm" in point and "y_mm" in point:
                                    points.append(
                                        {
                                            "x_mm": float(point.get("x_mm", 0.0)),
                                            "y_mm": float(point.get("y_mm", 0.0)),
                                        }
                                    )
                                    continue
                                if "x_local" in point and "y_local" in point:
                                    points.append(
                                        {
                                            "x_mm": unit_norm.scalar_to_mm(float(point.get("x_local", 0.0))),
                                            "y_mm": unit_norm.scalar_to_mm(float(point.get("y_local", 0.0))),
                                        }
                                    )
                                    continue
                                if "x" in point and "y" in point:
                                    x_mm = unit_norm.scalar_to_mm(float(point["x"]))
                                    y_mm = unit_norm.scalar_to_mm(float(point["y"]))
                                    points.append({"x_mm": x_mm, "y_mm": y_mm})
                                    continue
                            if isinstance(point, (list, tuple)) and len(point) >= 2:
                                x_mm = unit_norm.scalar_to_mm(float(point[0]))
                                y_mm = unit_norm.scalar_to_mm(float(point[1]))
                                points.append({"x_mm": x_mm, "y_mm": y_mm})
                    if len(points) >= 2:
                        if "width_mm" in item:
                            width_mm = float(item.get("width_mm", 0.2))
                        elif "width_local" in item:
                            width_mm = unit_norm.scalar_to_mm(float(item.get("width_local", 0.2)))
                        else:
                            width_mm = unit_norm.scalar_to_mm(float(item.get("width", 0.2)))
                        outline.append(
                            {
                                "kind": "wire_path",
                                "layer": layer,
                                "width_mm": width_mm,
                                "points": points,
                            }
                        )
                    continue

                if kind == "polygon":
                    points: list[dict[str, float]] = []
                    raw_points = item.get("points")
                    if isinstance(raw_points, list):
                        for point in raw_points:
                            if isinstance(point, dict):
                                if "x_mm" in point and "y_mm" in point:
                                    points.append(
                                        {
                                            "x_mm": float(point.get("x_mm", 0.0)),
                                            "y_mm": float(point.get("y_mm", 0.0)),
                                        }
                                    )
                                    continue
                                if "x_local" in point and "y_local" in point:
                                    points.append(
                                        {
                                            "x_mm": unit_norm.scalar_to_mm(float(point.get("x_local", 0.0))),
                                            "y_mm": unit_norm.scalar_to_mm(float(point.get("y_local", 0.0))),
                                        }
                                    )
                                    continue
                                if "x" in point and "y" in point:
                                    x_mm = unit_norm.scalar_to_mm(float(point["x"]))
                                    y_mm = unit_norm.scalar_to_mm(float(point["y"]))
                                    points.append({"x_mm": x_mm, "y_mm": y_mm})
                                    continue
                            if isinstance(point, (list, tuple)) and len(point) >= 2:
                                x_mm = unit_norm.scalar_to_mm(float(point[0]))
                                y_mm = unit_norm.scalar_to_mm(float(point[1]))
                                points.append({"x_mm": x_mm, "y_mm": y_mm})
                    if len(points) >= 3:
                        if "width_mm" in item:
                            width_mm = float(item.get("width_mm", 0.2))
                        elif "width_local" in item:
                            width_mm = unit_norm.scalar_to_mm(float(item.get("width_local", 0.2)))
                        else:
                            width_mm = unit_norm.scalar_to_mm(float(item.get("width", 0.2)))
                        outline.append(
                            {
                                "kind": "polygon",
                                "layer": layer,
                                "width_mm": width_mm,
                                "points": points,
                            }
                        )
                    continue

                if kind == "hole":
                    if "x_mm" in item and "y_mm" in item:
                        x_mm = float(item.get("x_mm", 0.0))
                        y_mm = float(item.get("y_mm", 0.0))
                    elif "x_local" in item and "y_local" in item:
                        x_mm = unit_norm.scalar_to_mm(float(item.get("x_local", 0.0)))
                        y_mm = unit_norm.scalar_to_mm(float(item.get("y_local", 0.0)))
                    else:
                        x_mm = unit_norm.scalar_to_mm(float(item.get("x", 0.0)))
                        y_mm = unit_norm.scalar_to_mm(float(item.get("y", 0.0)))
                    if "drill_mm" in item:
                        drill_mm = float(item.get("drill_mm", 0.0))
                    elif "drill_local" in item:
                        drill_mm = unit_norm.scalar_to_mm(float(item.get("drill_local", 0.0)))
                    else:
                        drill_mm = unit_norm.scalar_to_mm(float(item.get("drill", 0.0)))
                    if drill_mm > 0.0:
                        outline.append(
                            {
                                "kind": "hole",
                                "x_mm": x_mm,
                                "y_mm": y_mm,
                                "drill_mm": drill_mm,
                                "layer": layer,
                            }
                        )
                    continue

                if kind == "text":
                    if "x_mm" in item and "y_mm" in item:
                        x_mm = float(item.get("x_mm", 0.0))
                        y_mm = float(item.get("y_mm", 0.0))
                    elif "x_local" in item and "y_local" in item:
                        x_mm = unit_norm.scalar_to_mm(float(item.get("x_local", 0.0)))
                        y_mm = unit_norm.scalar_to_mm(float(item.get("y_local", 0.0)))
                    else:
                        x_mm = unit_norm.scalar_to_mm(float(item.get("x", 0.0)))
                        y_mm = unit_norm.scalar_to_mm(float(item.get("y", 0.0)))
                    if item.get("size_mm") is not None:
                        size_mm = float(item.get("size_mm", 1.0))
                    elif "size_local" in item:
                        size_mm = unit_norm.scalar_to_mm(float(item.get("size_local", 1.0)))
                    else:
                        size_mm = unit_norm.scalar_to_mm(float(item.get("size", 20.0)))
                    outline.append(
                        {
                            "kind": "text",
                            "layer": layer,
                            "text": str(item.get("text") or ""),
                            "x_mm": x_mm,
                            "y_mm": y_mm,
                            "size_mm": size_mm,
                            "rotation_deg": float(item.get("rotation", 0.0) or 0.0),
                        }
                    )

        return Package(package_id=package_id, name=package_name, pads=pads, outline=outline)

    @staticmethod
    def _package_pad_from_obj(obj: dict[str, Any], unit_norm: UnitNormalizer) -> Pad:
        if "x_mm" in obj and "y_mm" in obj:
            x = float(obj.get("x_mm", 0.0))
            y = float(obj.get("y_mm", 0.0))
        elif "x_local" in obj and "y_local" in obj:
            x = unit_norm.scalar_to_mm(float(obj.get("x_local", 0.0)))
            y = unit_norm.scalar_to_mm(float(obj.get("y_local", 0.0)))
        else:
            x = unit_norm.scalar_to_mm(float(obj.get("x", obj.get("pos_x", 0.0))))
            y = unit_norm.scalar_to_mm(float(obj.get("y", obj.get("pos_y", 0.0))))

        if "width_mm" in obj:
            width_mm = float(obj.get("width_mm", 1.0))
        elif "width_local" in obj:
            width_mm = unit_norm.scalar_to_mm(float(obj.get("width_local", 1.0)))
        else:
            width_mm = unit_norm.scalar_to_mm(float(obj.get("width", obj.get("w", 1.0))))

        if "height_mm" in obj:
            height_mm = float(obj.get("height_mm", 1.0))
        elif "height_local" in obj:
            height_mm = unit_norm.scalar_to_mm(float(obj.get("height_local", 1.0)))
        else:
            height_mm = unit_norm.scalar_to_mm(float(obj.get("height", obj.get("h", 1.0))))

        if obj.get("drill") is None and obj.get("drill_local") is None and obj.get("drill_mm") is None:
            drill_mm = None
        elif "drill_mm" in obj:
            drill_mm = float(obj.get("drill_mm", 0.0))
        elif "drill_local" in obj:
            drill_mm = unit_norm.scalar_to_mm(float(obj.get("drill_local", 0.0)))
        else:
            drill_mm = unit_norm.scalar_to_mm(float(obj.get("drill", 0.0)))

        return Pad(
            pad_number=str(obj.get("number") or obj.get("pad") or obj.get("name") or ""),
            at=Point(x, y),
            shape=str(obj.get("shape", "rect")),
            width_mm=width_mm,
            height_mm=height_mm,
            drill_mm=drill_mm,
            layer=str(obj.get("layer", "top_copper")),
            rotation_deg=float(obj.get("rotation", obj.get("rot", 0.0)) or 0.0),
            net=_empty_to_none(obj.get("net") or obj.get("net_name")),
        )

    @staticmethod
    def _arc_from_obj(obj: dict[str, Any], unit_norm: UnitNormalizer) -> Arc:
        sx, sy = _point_pair(obj, unit_norm, start_keys=("x1", "y1"), fallback=("sx", "sy"))
        ex, ey = _point_pair(obj, unit_norm, start_keys=("x2", "y2"), fallback=("ex", "ey"))
        cx, cy = _point_pair(obj, unit_norm, start_keys=("cx", "cy"), fallback=("center_x", "center_y"))
        return Arc(
            start=Point(sx, sy),
            end=Point(ex, ey),
            center=Point(cx, cy),
            width_mm=unit_norm.scalar_to_mm(float(obj.get("width", 0.2))),
            layer=str(obj.get("layer", "top_copper")),
            net=_empty_to_none(obj.get("net") or obj.get("net_name")),
        )

    @staticmethod
    def _region_from_obj(obj: dict[str, Any], unit_norm: UnitNormalizer, kind: str) -> Region:
        points: list[Point] = []
        raw_points = obj.get("points")

        if isinstance(raw_points, list):
            for point in raw_points:
                if isinstance(point, dict):
                    x, y = _point_from_obj(point, unit_norm)
                    points.append(Point(x, y))
                elif isinstance(point, (list, tuple)) and len(point) >= 2:
                    x, y = unit_norm.to_mm(point[0], point[1])
                    points.append(Point(x, y))
        else:
            poly = obj.get("polygon")
            if isinstance(poly, list):
                for point in poly:
                    if isinstance(point, (list, tuple)) and len(point) >= 2:
                        x, y = unit_norm.to_mm(point[0], point[1])
                        points.append(Point(x, y))

        return Region(
            region_id=str(obj.get("id") or obj.get("name") or kind),
            layer=str(obj.get("layer", "top_copper")),
            points=points,
            net=_empty_to_none(obj.get("net") or obj.get("net_name")),
            kind=kind,
        )

    @staticmethod
    def _hole_from_obj(obj: dict[str, Any], unit_norm: UnitNormalizer):
        x, y = _point_from_obj(obj, unit_norm)
        plated = bool(obj.get("plated", obj.get("pth", False)))

        return Hole(
            at=Point(x, y),
            drill_mm=unit_norm.scalar_to_mm(float(obj.get("drill", 0.8))),
            plated=plated,
        )

    @staticmethod
    def _text_from_obj(obj: dict[str, Any], unit_norm: UnitNormalizer, layer: str) -> TextItem:
        x, y = _point_from_obj(obj, unit_norm)
        if obj.get("size_mm") is not None:
            size_mm = float(obj.get("size_mm", 1.2))
        else:
            size_mm = unit_norm.scalar_to_mm(float(obj.get("size", obj.get("font_size", 1.2))))
        return TextItem(
            text=str(obj.get("text") or obj.get("value") or ""),
            at=Point(x, y),
            layer=layer,
            size_mm=size_mm,
            rotation_deg=float(obj.get("rotation", obj.get("rot", 0.0)) or 0.0),
            mirrored=bool(obj.get("mirrored", False)),
        )

    @staticmethod
    def _attach_default_board_layers(project: Project) -> None:
        if project.board is None:
            return
        if not project.board.layers:
            project.board.layers.extend(project.layers)

    @staticmethod
    def _ensure_board_outline(project: Project, board: Board) -> None:
        if board.outline:
            return
        if not board.regions:
            return

        candidates: list[tuple[float, Region]] = []
        for region in board.regions:
            if len(region.points) < 3:
                continue
            layer_token = str(region.layer or "").strip().lower()
            if layer_token not in {
                "1",
                "2",
                "top",
                "bottom",
                "top_copper",
                "bottom_copper",
                "toplayer",
                "bottomlayer",
            }:
                continue
            area = abs(_polygon_area_mm2(region.points))
            if area <= 0.0:
                continue
            candidates.append((area, region))

        if not candidates:
            return
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        best_region = candidates[0][1]
        board.outline.append(
            Region(
                region_id=f"inferred_outline_{best_region.region_id}",
                layer="dimension",
                points=[Point(point.x_mm, point.y_mm) for point in best_region.points],
                net=None,
                kind="outline",
            )
        )
        project.events.append(
            project_event(
                Severity.WARNING,
                "OUTLINE_INFERRED_FROM_REGION",
                "Board outline inferred from largest copper region because explicit outline was missing",
                {
                    "source_region_id": best_region.region_id,
                    "source_layer": best_region.layer,
                },
            )
        )

    @staticmethod
    def _harmonize_legacy_package_local_frames(project: Project) -> None:
        if project.source_format != SourceFormat.EASYEDA_STD:
            return
        if not bool(project.metadata.get("legacy_shape_string_mode", False)):
            return
        if project.board is None or not project.board.pads:
            return

        package_lookup: dict[str, Package] = {}
        for package in project.packages:
            package_lookup[package.package_id] = package
            if package.name:
                package_lookup[package.name] = package

        existing_package_ids = {str(package.package_id) for package in project.packages}
        existing_package_names = {str(package.name) for package in project.packages if package.name}

        components_by_package: dict[str, list[Component]] = {}
        for component in project.components:
            package_id = str(component.package_id or "").strip()
            if not package_id:
                continue
            package = package_lookup.get(package_id)
            if package is None or not package.pads:
                continue
            components_by_package.setdefault(package.package_id, []).append(component)

        board_pad_points = [
            (
                str(pad.pad_number or "").strip(),
                float(pad.at.x_mm),
                float(pad.at.y_mm),
            )
            for pad in project.board.pads
        ]
        board_pad_points_by_component: dict[str, list[tuple[str, float, float]]] = {}
        for pad in project.board.pads:
            key = _pad_component_key(pad)
            if not key:
                continue
            board_pad_points_by_component.setdefault(key, []).append(
                (
                    str(pad.pad_number or "").strip(),
                    float(pad.at.x_mm),
                    float(pad.at.y_mm),
                )
            )
        variants = [(False, False), (True, False), (False, True), (True, True)]
        mirror_applied: list[str] = []
        split_applied: list[str] = []

        for package_id, components in components_by_package.items():
            package = package_lookup.get(package_id)
            if package is None or len(package.pads) < 2:
                continue

            package_span = _package_span_mm(package)
            search_radius_mm = max(8.0, package_span + 2.5)
            selected_variant_by_component: dict[str, tuple[bool, bool]] = {}
            for component in components:
                component_key = _component_instance_key(component)
                component_board_pad_points = (
                    board_pad_points_by_component.get(component_key, [])
                    if component_key
                    else []
                )
                candidate_board_pad_points = (
                    component_board_pad_points if component_board_pad_points else board_pad_points
                )
                variant_scores: list[tuple[float, bool, bool]] = []
                for mirror_x, mirror_y in variants:
                    score = _component_package_variant_fit_score(
                        component=component,
                        package=package,
                        board_pad_points=candidate_board_pad_points,
                        search_radius_mm=search_radius_mm,
                        mirror_x=mirror_x,
                        mirror_y=mirror_y,
                    )
                    variant_scores.append((score, mirror_x, mirror_y))

                if not variant_scores:
                    continue
                variant_scores.sort(key=lambda item: item[0])
                best_score, best_mx, best_my = variant_scores[0]
                identity_score = next(
                    (score for score, mx, my in variant_scores if not mx and not my),
                    None,
                )
                chosen: tuple[bool, bool] = (False, False)
                if identity_score is not None:
                    if (
                        (best_mx or best_my)
                        and best_score < 0.35
                        and (identity_score - best_score) >= 0.20
                    ):
                        chosen = (best_mx, best_my)
                if component.source_instance_id:
                    selected_variant_by_component[str(component.source_instance_id)] = chosen
                else:
                    selected_variant_by_component[f"{component.refdes}@{component.at.x_mm:.6f},{component.at.y_mm:.6f}"] = chosen

            if not selected_variant_by_component:
                continue

            grouped: dict[tuple[bool, bool], list[Component]] = {}
            for component in components:
                key = (
                    str(component.source_instance_id)
                    if component.source_instance_id
                    else f"{component.refdes}@{component.at.x_mm:.6f},{component.at.y_mm:.6f}"
                )
                variant = selected_variant_by_component.get(key, (False, False))
                grouped.setdefault(variant, []).append(component)

            if _package_distinct_pad_count(package) == 2 and (False, False) in grouped:
                non_identity_groups = [
                    (variant, grouped_components)
                    for variant, grouped_components in grouped.items()
                    if variant != (False, False) and grouped_components
                ]
                if len(non_identity_groups) == 1:
                    dominant_variant, dominant_components = non_identity_groups[0]
                    identity_components = grouped.get((False, False), [])
                    if len(dominant_components) > len(identity_components):
                        # For symmetric two-pin packages, pad-only fitting can
                        # legitimately choose either of two 180-equivalent local
                        # frames. Canonicalize package base orientation to the
                        # dominant instance variant so most placements remain in
                        # identity frame, then remap variant assignments.
                        _mirror_package_local_frame(
                            package,
                            mirror_x=dominant_variant[0],
                            mirror_y=dominant_variant[1],
                        )
                        remapped: dict[tuple[bool, bool], list[Component]] = {}
                        for variant, grouped_components in grouped.items():
                            remapped_variant = (
                                bool(variant[0]) ^ bool(dominant_variant[0]),
                                bool(variant[1]) ^ bool(dominant_variant[1]),
                            )
                            remapped.setdefault(remapped_variant, []).extend(grouped_components)
                        grouped = remapped
                        mirror_applied.append(
                            f"{package.package_id}:BASE->{_mirror_variant_suffix(dominant_variant[0], dominant_variant[1])}"
                        )

            non_identity = {
                variant: comps
                for variant, comps in grouped.items()
                if variant != (False, False) and comps
            }
            if not non_identity:
                continue

            # If every instance agrees on a mirrored frame, mutate in place.
            if len(non_identity) == 1 and (False, False) not in grouped:
                (best_mx, best_my), _ = next(iter(non_identity.items()))
                _mirror_package_local_frame(package, mirror_x=best_mx, mirror_y=best_my)
                mirror_applied.append(
                    f"{package.package_id}:{_mirror_variant_suffix(best_mx, best_my)}"
                )
                continue

            # Mixed per-instance frames: clone mirrored package variants and
            # rebind only the affected component instances.
            for (mirror_x, mirror_y), grouped_components in sorted(
                non_identity.items(),
                key=lambda item: _mirror_variant_suffix(item[0][0], item[0][1]),
            ):
                suffix = _mirror_variant_suffix(mirror_x, mirror_y)
                variant_package = copy.deepcopy(package)
                variant_package.package_id = _allocate_package_variant_id(
                    package.package_id,
                    suffix,
                    existing_package_ids,
                )
                variant_package.name = _allocate_package_variant_name(
                    package.name or package.package_id,
                    suffix,
                    existing_package_names,
                )
                _mirror_package_local_frame(variant_package, mirror_x=mirror_x, mirror_y=mirror_y)
                project.packages.append(variant_package)
                package_lookup[variant_package.package_id] = variant_package
                package_lookup[variant_package.name] = variant_package
                for component in grouped_components:
                    component.package_id = variant_package.package_id
                refs = ",".join(sorted(component.refdes for component in grouped_components))
                split_applied.append(f"{package.package_id}:{suffix}->{variant_package.package_id} [{refs}]")

        if mirror_applied:
            project.events.append(
                project_event(
                    Severity.INFO,
                    "LEGACY_PACKAGE_LOCAL_FRAME_MIRRORED",
                    "Adjusted legacy STD package local frames to preserve board placement fidelity",
                    {
                        "count": len(mirror_applied),
                        "packages": mirror_applied,
                    },
                )
            )
        if split_applied:
            project.events.append(
                project_event(
                    Severity.INFO,
                    "LEGACY_PACKAGE_LOCAL_FRAME_VARIANT_SPLIT",
                    "Split legacy STD package variants by mirrored local frame to preserve per-instance placement fidelity",
                    {
                        "count": len(split_applied),
                        "variants": split_applied,
                    },
                )
            )


def _obj_type(obj: dict[str, Any]) -> str:
    typ = obj.get("type") or obj.get("kind") or obj.get("shape") or obj.get("obj")
    return str(typ).strip().lower()


def _component_instance_key(component: Component) -> str:
    source_id = str(component.source_instance_id or "").strip()
    if source_id:
        return f"ID:{source_id}"
    refdes = str(component.refdes or "").strip()
    if refdes:
        return f"REF:{refdes}"
    return ""


def _pad_component_key(pad: Pad) -> str:
    source_id = str(pad.source_instance_id or "").strip()
    if source_id:
        return f"ID:{source_id}"
    refdes = str(pad.component_refdes or "").strip()
    if refdes:
        return f"REF:{refdes}"
    return ""


def _package_span_mm(package: Package) -> float:
    if not package.pads:
        return 0.0
    xs = [float(pad.at.x_mm) for pad in package.pads]
    ys = [float(pad.at.y_mm) for pad in package.pads]
    span_x = max(xs) - min(xs) if xs else 0.0
    span_y = max(ys) - min(ys) if ys else 0.0
    return max(span_x, span_y)


def _package_distinct_pad_count(package: Package) -> int:
    values = {str(pad.pad_number or "").strip() for pad in package.pads if str(pad.pad_number or "").strip()}
    return len(values)


def _component_package_world_points(
    component: Component,
    package: Package,
    mirror_x: bool,
    mirror_y: bool,
) -> list[tuple[str, float, float]]:
    out: list[tuple[str, float, float]] = []
    angle = math.radians(float(component.rotation_deg or 0.0))
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    cx = float(component.at.x_mm)
    cy = float(component.at.y_mm)

    for pad in package.pads:
        px = float(pad.at.x_mm)
        py = float(pad.at.y_mm)
        if mirror_x:
            px = -px
        if mirror_y:
            py = -py
        if component.side == Side.BOTTOM:
            px = -px
        rx = px * cos_a - py * sin_a
        ry = px * sin_a + py * cos_a
        out.append((str(pad.pad_number or "").strip(), cx + rx, cy + ry))
    return out


def _nearby_points(
    points: list[tuple[str, float, float]],
    center_x: float,
    center_y: float,
    radius_mm: float,
) -> list[tuple[str, float, float]]:
    if radius_mm <= 0.0:
        return []
    out: list[tuple[str, float, float]] = []
    radius_sq = float(radius_mm) * float(radius_mm)
    for pad_number, px, py in points:
        dx = float(px) - float(center_x)
        dy = float(py) - float(center_y)
        if dx * dx + dy * dy <= radius_sq:
            out.append((str(pad_number or "").strip(), float(px), float(py)))
    return out


def _pad_aware_mean_distance(
    source: list[tuple[str, float, float]],
    targets: list[tuple[str, float, float]],
) -> float:
    if not source:
        return float("inf")
    if not targets:
        return float("inf")
    total = 0.0
    for source_pad_number, sx, sy in source:
        same_number_targets = [
            (tx, ty)
            for target_pad_number, tx, ty in targets
            if source_pad_number and target_pad_number == source_pad_number
        ]
        active_targets = same_number_targets or [(tx, ty) for _, tx, ty in targets]
        best = min(math.hypot(float(sx) - float(tx), float(sy) - float(ty)) for tx, ty in active_targets)
        total += best
    return total / float(len(source))


def _component_package_variant_fit_score(
    component: Component,
    package: Package,
    board_pad_points: list[tuple[str, float, float]],
    search_radius_mm: float,
    mirror_x: bool,
    mirror_y: bool,
) -> float:
    world_points = _component_package_world_points(
        component=component,
        package=package,
        mirror_x=mirror_x,
        mirror_y=mirror_y,
    )
    if not world_points:
        return float("inf")

    local_board = _nearby_points(
        board_pad_points,
        center_x=float(component.at.x_mm),
        center_y=float(component.at.y_mm),
        radius_mm=search_radius_mm,
    )
    targets = local_board if local_board else board_pad_points
    return _pad_aware_mean_distance(world_points, targets)


def _mirror_variant_suffix(mirror_x: bool, mirror_y: bool) -> str:
    if mirror_x and mirror_y:
        return "MXMY"
    if mirror_x:
        return "MX"
    if mirror_y:
        return "MY"
    return "ID"


def _allocate_package_variant_id(base_id: str, suffix: str, existing_ids: set[str]) -> str:
    candidate = f"{base_id}:{suffix}"
    if candidate not in existing_ids:
        existing_ids.add(candidate)
        return candidate
    idx = 2
    while True:
        token = f"{candidate}_{idx}"
        if token not in existing_ids:
            existing_ids.add(token)
            return token
        idx += 1


def _allocate_package_variant_name(base_name: str, suffix: str, existing_names: set[str]) -> str:
    candidate = f"{base_name}:{suffix}"
    if candidate not in existing_names:
        existing_names.add(candidate)
        return candidate
    idx = 2
    while True:
        token = f"{candidate}_{idx}"
        if token not in existing_names:
            existing_names.add(token)
            return token
        idx += 1


def _mirror_angle_deg(angle_deg: float, mirror_x: bool, mirror_y: bool) -> float:
    angle = float(angle_deg or 0.0) % 360.0
    if mirror_x:
        angle = (180.0 - angle) % 360.0
    if mirror_y:
        angle = (-angle) % 360.0
    return angle


def _mirror_package_local_frame(package: Package, mirror_x: bool, mirror_y: bool) -> None:
    if not (mirror_x or mirror_y):
        return

    for pad in package.pads:
        x = -float(pad.at.x_mm) if mirror_x else float(pad.at.x_mm)
        y = -float(pad.at.y_mm) if mirror_y else float(pad.at.y_mm)
        pad.at = Point(x, y)
        pad.rotation_deg = _mirror_angle_deg(float(pad.rotation_deg or 0.0), mirror_x, mirror_y)

    for item in package.outline:
        kind = str(item.get("kind") or "").strip().lower()
        if kind in {"wire_path", "polygon"}:
            points = item.get("points")
            if isinstance(points, list):
                for point in points:
                    if not isinstance(point, dict):
                        continue
                    if "x_mm" in point:
                        point["x_mm"] = -float(point.get("x_mm", 0.0)) if mirror_x else float(point.get("x_mm", 0.0))
                    if "y_mm" in point:
                        point["y_mm"] = -float(point.get("y_mm", 0.0)) if mirror_y else float(point.get("y_mm", 0.0))
        elif kind == "hole":
            if "x_mm" in item:
                item["x_mm"] = -float(item.get("x_mm", 0.0)) if mirror_x else float(item.get("x_mm", 0.0))
            if "y_mm" in item:
                item["y_mm"] = -float(item.get("y_mm", 0.0)) if mirror_y else float(item.get("y_mm", 0.0))
        elif kind == "text":
            if "x_mm" in item:
                item["x_mm"] = -float(item.get("x_mm", 0.0)) if mirror_x else float(item.get("x_mm", 0.0))
            if "y_mm" in item:
                item["y_mm"] = -float(item.get("y_mm", 0.0)) if mirror_y else float(item.get("y_mm", 0.0))
            item["rotation_deg"] = _mirror_angle_deg(
                float(item.get("rotation_deg", 0.0) or 0.0),
                mirror_x,
                mirror_y,
            )


def _point_from_obj(obj: dict[str, Any], unit_norm: UnitNormalizer) -> tuple[float, float]:
    x = obj.get("x", obj.get("pos_x", 0.0))
    y = obj.get("y", obj.get("pos_y", 0.0))
    return unit_norm.to_mm(float(x), float(y))


def _point_pair(
    obj: dict[str, Any],
    unit_norm: UnitNormalizer,
    start_keys: tuple[str, str],
    fallback: tuple[str, str],
) -> tuple[float, float]:
    x = obj.get(start_keys[0], obj.get(fallback[0], 0.0))
    y = obj.get(start_keys[1], obj.get(fallback[1], 0.0))
    return unit_norm.to_mm(float(x), float(y))


def _normalize_wire(obj: dict[str, Any], unit_norm: UnitNormalizer) -> dict[str, Any]:
    sx, sy = _point_pair(obj, unit_norm, ("x1", "y1"), ("sx", "sy"))
    ex, ey = _point_pair(obj, unit_norm, ("x2", "y2"), ("ex", "ey"))
    return {
        "x1_mm": sx,
        "y1_mm": sy,
        "x2_mm": ex,
        "y2_mm": ey,
        "net": obj.get("net") or obj.get("net_name"),
        "layer": obj.get("layer", "schematic_wire"),
    }


def _slug(text: Any) -> str:
    text = str(text).strip().lower()
    text = re.sub(r"[^a-z0-9_\-]+", "_", text)
    return text.strip("_") or "project"


def _empty_to_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _polygon_area_mm2(points: list[Point]) -> float:
    if len(points) < 3:
        return 0.0
    area2 = 0.0
    for idx, point in enumerate(points):
        nxt = points[(idx + 1) % len(points)]
        area2 += point.x_mm * nxt.y_mm - nxt.x_mm * point.y_mm
    return 0.5 * area2


def _upsert_component(components: list[Component], incoming: Component) -> None:
    for component in components:
        same_instance = bool(
            component.source_instance_id
            and incoming.source_instance_id
            and component.source_instance_id == incoming.source_instance_id
        )
        same_refdes = component.refdes == incoming.refdes and _can_merge_by_refdes(component, incoming)
        if same_instance or same_refdes:
            # Schematic attributes override board-only placeholders where available.
            if component.value == "" and incoming.value:
                component.value = incoming.value
            for attr_key, attr_value in incoming.attributes.items():
                component.attributes.setdefault(attr_key, attr_value)
            if component.mpn is None and incoming.mpn:
                component.mpn = incoming.mpn
            if component.manufacturer is None and incoming.manufacturer:
                component.manufacturer = incoming.manufacturer
            if component.package_id is None and incoming.package_id:
                component.package_id = incoming.package_id
            if component.symbol_id is None and incoming.symbol_id:
                component.symbol_id = incoming.symbol_id
            if component.device_id is None and incoming.device_id:
                component.device_id = incoming.device_id
            if component.sheet_id is None and incoming.sheet_id:
                component.sheet_id = incoming.sheet_id
            if component.source_instance_id is None and incoming.source_instance_id:
                component.source_instance_id = incoming.source_instance_id
            component.at = incoming.at
            component.rotation_deg = incoming.rotation_deg
            component.side = incoming.side
            return
    components.append(incoming)


def _can_merge_by_refdes(existing: Component, incoming: Component) -> bool:
    if existing.refdes != incoming.refdes:
        return False

    existing_id = str(existing.source_instance_id or "").strip()
    incoming_id = str(incoming.source_instance_id or "").strip()
    if existing_id and incoming_id:
        return existing_id == incoming_id

    # If one side has a stable source instance ID and the other does not,
    # merge only when additional identity fields are compatible.
    return _components_are_compatible(existing, incoming)


def _components_are_compatible(existing: Component, incoming: Component) -> bool:
    matches = 0

    package_match = _compatible_identity(existing.package_id, incoming.package_id)
    if package_match is False:
        return False
    if package_match is True:
        matches += 1

    symbol_match = _compatible_identity(existing.symbol_id, incoming.symbol_id)
    if symbol_match is False:
        return False
    if symbol_match is True:
        matches += 1

    device_match = _compatible_identity(existing.device_id, incoming.device_id)
    if device_match is False:
        return False
    if device_match is True:
        matches += 1

    source_match = _compatible_identity(existing.source_name, incoming.source_name)
    if source_match is False:
        return False
    if source_match is True:
        matches += 1

    # Require at least one concrete identity match when source instance IDs are missing.
    return matches > 0


def _compatible_identity(left: Any, right: Any) -> bool | None:
    l = _identity_token(left)
    r = _identity_token(right)
    if not l or not r:
        return None
    return l == r


def _identity_token(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    return re.sub(r"[^A-Z0-9]+", "", text)


def _upsert_symbol(symbols: list[Symbol], incoming: Symbol) -> None:
    for symbol in symbols:
        if symbol.symbol_id == incoming.symbol_id:
            if not symbol.pins and incoming.pins:
                symbol.pins = incoming.pins
            return
    symbols.append(incoming)


def _upsert_package(packages: list[Package], incoming: Package) -> None:
    for package in packages:
        if package.package_id == incoming.package_id:
            if not package.pads and incoming.pads:
                package.pads = incoming.pads
            if not package.outline and incoming.outline:
                package.outline = incoming.outline
            return
    packages.append(incoming)


def _upsert_net(nets: list[Net], incoming: Net) -> None:
    for net in nets:
        if net.name == incoming.name:
            existing_nodes = {(node.refdes, node.pin) for node in net.nodes}
            for node in incoming.nodes:
                key = (node.refdes, node.pin)
                if key not in existing_nodes:
                    net.nodes.append(node)
            return
    nets.append(incoming)
