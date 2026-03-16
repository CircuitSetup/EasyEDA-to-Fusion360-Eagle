from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from easyeda2fusion.builders.component_identity import resolve_component_refdes as _shared_resolve_component_refdes
from easyeda2fusion.model import Component, Project, Side, Symbol


@dataclass(frozen=True)
class CanonicalSymbolPin:
    pin_id: str
    pin_name: str
    pin_number: str
    endpoint_x_mm: float
    endpoint_y_mm: float
    outward_dx: float
    outward_dy: float
    label_x_mm: float
    label_y_mm: float
    length_mm: float | None = None


@dataclass(frozen=True)
class CanonicalSymbolGeometry:
    symbol_id: str
    symbol_name: str
    source_type: str
    origin_x_mm: float
    origin_y_mm: float
    min_x_mm: float
    min_y_mm: float
    max_x_mm: float
    max_y_mm: float
    pins: tuple[CanonicalSymbolPin, ...]


@dataclass(frozen=True)
class SymbolOriginRecord:
    refdes: str
    component_instance_id: str
    symbol_id: str
    symbol_source_type: str
    symbol_local_origin_x_mm: float
    symbol_local_origin_y_mm: float
    schematic_origin_x_mm: float
    schematic_origin_y_mm: float
    rotation_deg: float
    mirrored: bool


@dataclass(frozen=True)
class PlacedPinAnchor:
    refdes: str
    pin_id: str
    pin_name: str
    pin_number: str
    endpoint_x_mm: float
    endpoint_y_mm: float
    outward_dx: float
    outward_dy: float
    symbol_id: str
    symbol_source_type: str


@dataclass
class SchematicGeometryMaps:
    symbol_definitions: dict[str, CanonicalSymbolGeometry]
    symbol_origins: dict[str, SymbolOriginRecord]
    placed_pin_anchors: dict[tuple[str, str], PlacedPinAnchor]

    def symbol_geometry_report(self) -> dict[str, Any]:
        definitions = [
            self.symbol_definitions[key]
            for key in sorted(self.symbol_definitions.keys())
        ]
        return {
            "symbol_count": len(definitions),
            "symbols": [
                {
                    "symbol_id": item.symbol_id,
                    "symbol_name": item.symbol_name,
                    "source_type": item.source_type,
                    "origin": {"x_mm": item.origin_x_mm, "y_mm": item.origin_y_mm},
                    "body_bounds": {
                        "min_x_mm": item.min_x_mm,
                        "min_y_mm": item.min_y_mm,
                        "max_x_mm": item.max_x_mm,
                        "max_y_mm": item.max_y_mm,
                    },
                    "pins": [
                        {
                            "pin_id": pin.pin_id,
                            "pin_name": pin.pin_name,
                            "pin_number": pin.pin_number,
                            "endpoint_mm": {"x": pin.endpoint_x_mm, "y": pin.endpoint_y_mm},
                            "outward": {"dx": pin.outward_dx, "dy": pin.outward_dy},
                            "label_anchor_mm": {"x": pin.label_x_mm, "y": pin.label_y_mm},
                            "length_mm": pin.length_mm,
                        }
                        for pin in item.pins
                    ],
                }
                for item in definitions
            ],
        }

    def symbol_origin_report(self) -> dict[str, Any]:
        origins = [
            self.symbol_origins[key]
            for key in sorted(self.symbol_origins.keys())
        ]
        anchors = [
            self.placed_pin_anchors[key]
            for key in sorted(self.placed_pin_anchors.keys())
        ]
        return {
            "placed_symbol_count": len(origins),
            "placed_pin_anchor_count": len(anchors),
            "placed_symbols": [
                {
                    "refdes": item.refdes,
                    "component_instance_id": item.component_instance_id,
                    "symbol_id": item.symbol_id,
                    "symbol_source_type": item.symbol_source_type,
                    "symbol_local_origin_mm": {
                        "x": item.symbol_local_origin_x_mm,
                        "y": item.symbol_local_origin_y_mm,
                    },
                    "schematic_origin_mm": {
                        "x": item.schematic_origin_x_mm,
                        "y": item.schematic_origin_y_mm,
                    },
                    "rotation_deg": item.rotation_deg,
                    "mirrored": item.mirrored,
                }
                for item in origins
            ],
            "pin_anchors": [
                {
                    "refdes": item.refdes,
                    "pin_id": item.pin_id,
                    "pin_name": item.pin_name,
                    "pin_number": item.pin_number,
                    "symbol_id": item.symbol_id,
                    "symbol_source_type": item.symbol_source_type,
                    "endpoint_mm": {"x": item.endpoint_x_mm, "y": item.endpoint_y_mm},
                    "outward": {"dx": item.outward_dx, "dy": item.outward_dy},
                }
                for item in anchors
            ],
        }


def build_schematic_geometry_maps(
    project: Project,
    refdes_map: dict[str, str],
    placement_map: dict[str, tuple[float, float]],
    anchor_map: dict[str, dict[str, Any]],
    external_local_pin_map_by_ref: dict[str, dict[str, Any]],
    resolve_symbol_origin: Callable[[Any], tuple[float, float]],
    resolve_component_rotation: Callable[[Component], float],
) -> SchematicGeometryMaps:
    symbol_lookup = {str(symbol.symbol_id): symbol for symbol in project.symbols}
    generated_defs = _build_generated_symbol_definition_map(
        symbols=project.symbols,
        resolve_symbol_origin=resolve_symbol_origin,
    )

    symbol_definitions: dict[str, CanonicalSymbolGeometry] = dict(generated_defs)
    symbol_origins: dict[str, SymbolOriginRecord] = {}
    placed_pin_anchors: dict[tuple[str, str], PlacedPinAnchor] = {}

    external_symbol_defs_by_device: dict[str, CanonicalSymbolGeometry] = {}
    component_by_ref = {
        _resolve_component_refdes(component, refdes_map): component
        for component in project.components
    }

    for refdes in sorted(anchor_map.keys()):
        component = component_by_ref.get(refdes)
        if component is None:
            continue
        instance_id = _component_instance_id(component)
        placed_x, placed_y = placement_map.get(refdes, (component.at.x_mm, component.at.y_mm))
        rotation_deg = resolve_component_rotation(component)
        mirrored = component.side == Side.BOTTOM

        symbol_id = str(component.symbol_id or "").strip()
        symbol_source_type = "generated"
        if symbol_id and symbol_id in generated_defs:
            definition = generated_defs[symbol_id]
            symbol_key = symbol_id
            local_origin_x = definition.origin_x_mm
            local_origin_y = definition.origin_y_mm
            pin_lookup = {pin.pin_id: pin for pin in definition.pins}
        else:
            symbol_source_type = "external_library"
            device_token = str(component.device_id or "").strip() or f"external::{refdes}"
            symbol_key = f"external::{device_token}"
            if symbol_key not in external_symbol_defs_by_device:
                local_map = external_local_pin_map_by_ref.get(refdes, {})
                external_symbol_defs_by_device[symbol_key] = _external_symbol_definition(
                    symbol_key=symbol_key,
                    component=component,
                    local_pin_map=local_map,
                )
            definition = external_symbol_defs_by_device[symbol_key]
            local_origin_x = definition.origin_x_mm
            local_origin_y = definition.origin_y_mm
            pin_lookup = {pin.pin_id: pin for pin in definition.pins}

        symbol_definitions.setdefault(symbol_key, definition)
        symbol_origins[refdes] = SymbolOriginRecord(
            refdes=refdes,
            component_instance_id=instance_id,
            symbol_id=symbol_key,
            symbol_source_type=symbol_source_type,
            symbol_local_origin_x_mm=local_origin_x,
            symbol_local_origin_y_mm=local_origin_y,
            schematic_origin_x_mm=float(placed_x),
            schematic_origin_y_mm=float(placed_y),
            rotation_deg=float(rotation_deg),
            mirrored=bool(mirrored),
        )

        pin_map = anchor_map.get(refdes, {})
        for pin_id in sorted(pin_map.keys(), key=_pin_sort_key):
            anchor = pin_map[pin_id]
            pin_def = pin_lookup.get(pin_id)
            pin_name = pin_def.pin_name if pin_def is not None else pin_id
            pin_number = pin_def.pin_number if pin_def is not None else pin_id
            placed_pin_anchors[(refdes, pin_id)] = PlacedPinAnchor(
                refdes=refdes,
                pin_id=pin_id,
                pin_name=pin_name,
                pin_number=pin_number,
                endpoint_x_mm=float(getattr(anchor, "x_mm", 0.0)),
                endpoint_y_mm=float(getattr(anchor, "y_mm", 0.0)),
                outward_dx=float(getattr(anchor, "outward_dx", 0.0)),
                outward_dy=float(getattr(anchor, "outward_dy", 0.0)),
                symbol_id=symbol_key,
                symbol_source_type=symbol_source_type,
            )

    return SchematicGeometryMaps(
        symbol_definitions=symbol_definitions,
        symbol_origins=symbol_origins,
        placed_pin_anchors=placed_pin_anchors,
    )


def _build_generated_symbol_definition_map(
    symbols: list[Symbol],
    resolve_symbol_origin: Callable[[Any], tuple[float, float]],
) -> dict[str, CanonicalSymbolGeometry]:
    out: dict[str, CanonicalSymbolGeometry] = {}
    for symbol in sorted(symbols, key=lambda item: str(item.symbol_id or "")):
        symbol_id = str(symbol.symbol_id or "").strip()
        if not symbol_id:
            continue
        origin_x, origin_y = resolve_symbol_origin(symbol)
        pins: list[CanonicalSymbolPin] = []
        for pin in sorted(symbol.pins, key=lambda item: _pin_sort_key(str(item.pin_number or ""))):
            pin_id = str(pin.pin_number or "").strip()
            if not pin_id:
                continue
            pin_name = str(pin.pin_name or pin_id).strip() or pin_id
            local_x = float(pin.at.x_mm if pin.at else 0.0) - origin_x
            local_y = float(pin.at.y_mm if pin.at else 0.0) - origin_y
            outward_dx, outward_dy = _default_outward_direction(local_x, local_y)
            label_x = local_x + (outward_dx * 1.27)
            label_y = local_y + (outward_dy * 1.27)
            pins.append(
                CanonicalSymbolPin(
                    pin_id=pin_id,
                    pin_name=pin_name,
                    pin_number=pin_id,
                    endpoint_x_mm=local_x,
                    endpoint_y_mm=local_y,
                    outward_dx=outward_dx,
                    outward_dy=outward_dy,
                    label_x_mm=label_x,
                    label_y_mm=label_y,
                    length_mm=pin.length_mm,
                )
            )
        min_x, min_y, max_x, max_y = _symbol_bounds(symbol, origin_x, origin_y, pins)
        out[symbol_id] = CanonicalSymbolGeometry(
            symbol_id=symbol_id,
            symbol_name=str(symbol.name or symbol_id),
            source_type="generated",
            origin_x_mm=float(origin_x),
            origin_y_mm=float(origin_y),
            min_x_mm=min_x,
            min_y_mm=min_y,
            max_x_mm=max_x,
            max_y_mm=max_y,
            pins=tuple(pins),
        )
    return out


def _external_symbol_definition(
    symbol_key: str,
    component: Component,
    local_pin_map: dict[str, Any],
) -> CanonicalSymbolGeometry:
    pins: list[CanonicalSymbolPin] = []
    for pin_id in sorted(local_pin_map.keys(), key=_pin_sort_key):
        item = local_pin_map[pin_id]
        local_x = float(getattr(item, "x_mm", 0.0))
        local_y = float(getattr(item, "y_mm", 0.0))
        outward_dx = float(getattr(item, "outward_dx", 0.0))
        outward_dy = float(getattr(item, "outward_dy", 0.0))
        label_x = local_x + (outward_dx * 1.27)
        label_y = local_y + (outward_dy * 1.27)
        pins.append(
            CanonicalSymbolPin(
                pin_id=pin_id,
                pin_name=pin_id,
                pin_number=pin_id,
                endpoint_x_mm=local_x,
                endpoint_y_mm=local_y,
                outward_dx=outward_dx,
                outward_dy=outward_dy,
                label_x_mm=label_x,
                label_y_mm=label_y,
                length_mm=None,
            )
        )
    if pins:
        min_x = min(pin.endpoint_x_mm for pin in pins)
        min_y = min(pin.endpoint_y_mm for pin in pins)
        max_x = max(pin.endpoint_x_mm for pin in pins)
        max_y = max(pin.endpoint_y_mm for pin in pins)
    else:
        min_x = -2.54
        min_y = -2.54
        max_x = 2.54
        max_y = 2.54
    return CanonicalSymbolGeometry(
        symbol_id=symbol_key,
        symbol_name=str(component.device_id or symbol_key),
        source_type="external_library",
        origin_x_mm=0.0,
        origin_y_mm=0.0,
        min_x_mm=min_x,
        min_y_mm=min_y,
        max_x_mm=max_x,
        max_y_mm=max_y,
        pins=tuple(pins),
    )


def _symbol_bounds(
    symbol: Symbol,
    origin_x_mm: float,
    origin_y_mm: float,
    pins: list[CanonicalSymbolPin],
) -> tuple[float, float, float, float]:
    points: list[tuple[float, float]] = [
        (pin.endpoint_x_mm, pin.endpoint_y_mm)
        for pin in pins
    ]
    for graphic in symbol.graphics:
        if not isinstance(graphic, dict):
            continue
        if str(graphic.get("kind", "")).strip().lower() == "origin":
            continue
        points.extend(_graphic_points_local(graphic, origin_x_mm, origin_y_mm))
    if not points:
        return (-2.54, -2.54, 2.54, 2.54)
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _graphic_points_local(
    graphic: dict[str, Any],
    origin_x_mm: float,
    origin_y_mm: float,
) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    x_keys = ("x_mm", "x1_mm", "x2_mm", "x3_mm", "x4_mm")
    y_keys = ("y_mm", "y1_mm", "y2_mm", "y3_mm", "y4_mm")
    for x_key in x_keys:
        for y_key in y_keys:
            if x_key not in graphic or y_key not in graphic:
                continue
            try:
                points.append(
                    (
                        float(graphic[x_key]) - origin_x_mm,
                        float(graphic[y_key]) - origin_y_mm,
                    )
                )
            except Exception:
                continue
    return points


def _default_outward_direction(local_x_mm: float, local_y_mm: float) -> tuple[float, float]:
    if abs(local_x_mm) < 1e-9 and abs(local_y_mm) < 1e-9:
        return (-1.0, 0.0)
    if abs(local_x_mm) >= abs(local_y_mm):
        return (1.0 if local_x_mm > 0 else -1.0, 0.0)
    return (0.0, 1.0 if local_y_mm > 0 else -1.0)


def _resolve_component_refdes(component: Component, refdes_map: dict[str, str]) -> str:
    return _shared_resolve_component_refdes(component, refdes_map)


def _component_instance_id(component: Component) -> str:
    token = str(component.source_instance_id or "").strip()
    if token:
        return token
    return str(component.refdes or "").strip()


def _pin_sort_key(pin_id: str) -> tuple[int, str]:
    token = str(pin_id or "").strip()
    if token.isdigit():
        return (0, f"{int(token):09d}")
    return (1, token.upper())
