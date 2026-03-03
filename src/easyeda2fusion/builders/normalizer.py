from __future__ import annotations

import logging
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
                pads.append(Normalizer._pad_from_obj(raw_pad, unit_norm))

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
                            if not isinstance(point, (list, tuple)) or len(point) < 2:
                                continue
                            x_mm, y_mm = unit_norm.to_mm(point[0], point[1])
                            points.append({"x_mm": x_mm, "y_mm": y_mm})
                    if len(points) >= 2:
                        outline.append(
                            {
                                "kind": "wire_path",
                                "layer": layer,
                                "width_mm": unit_norm.scalar_to_mm(float(item.get("width", 0.2))),
                                "points": points,
                            }
                        )
                    continue

                if kind == "text":
                    x_mm, y_mm = unit_norm.to_mm(item.get("x", 0.0), item.get("y", 0.0))
                    outline.append(
                        {
                            "kind": "text",
                            "layer": layer,
                            "text": str(item.get("text") or ""),
                            "x_mm": x_mm,
                            "y_mm": y_mm,
                            "size_mm": unit_norm.scalar_to_mm(float(item.get("size", 20.0))),
                            "rotation_deg": float(item.get("rotation", 0.0) or 0.0),
                        }
                    )

        return Package(package_id=package_id, name=package_name, pads=pads, outline=outline)

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
        return TextItem(
            text=str(obj.get("text") or obj.get("value") or ""),
            at=Point(x, y),
            layer=layer,
            size_mm=unit_norm.scalar_to_mm(float(obj.get("size", obj.get("font_size", 1.2)))),
            rotation_deg=float(obj.get("rotation", obj.get("rot", 0.0)) or 0.0),
            mirrored=bool(obj.get("mirrored", False)),
        )

    @staticmethod
    def _attach_default_board_layers(project: Project) -> None:
        if project.board is None:
            return
        if not project.board.layers:
            project.board.layers.extend(project.layers)


def _obj_type(obj: dict[str, Any]) -> str:
    typ = obj.get("type") or obj.get("kind") or obj.get("shape") or obj.get("obj")
    return str(typ).strip().lower()


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
