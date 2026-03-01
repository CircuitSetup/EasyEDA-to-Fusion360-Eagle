from __future__ import annotations

from pathlib import Path
import re
from xml.dom import minidom
import xml.etree.ElementTree as ET

from easyeda2fusion.matchers.library_matcher import MatchContext
from easyeda2fusion.model import Device, Package, Pad, Project, Symbol, SymbolPin, Point


def emit_generated_library(
    match_ctx: MatchContext,
    out_dir: Path,
    project: Project | None = None,
) -> Path | None:
    supply_keys = _project_supply_keys(project)
    if not match_ctx.new_library_parts and not supply_keys:
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    library_path = out_dir / "easyeda_generated.lbr"

    packages: dict[str, Package] = {}
    symbols: dict[str, Symbol] = {}
    devices: dict[str, Device] = {}

    for item in match_ctx.new_library_parts:
        packages.setdefault(item.package.package_id, item.package)
        symbols.setdefault(item.symbol.symbol_id, item.symbol)
        devices.setdefault(item.device.device_id, item.device)

    supply_package_id = "PKG_PWR_SYMBOL"
    if supply_keys:
        packages.setdefault(
            supply_package_id,
            Package(
                package_id=supply_package_id,
                name=supply_package_id,
                pads=[
                    Pad(
                        pad_number="1",
                        at=Point(x_mm=0.0, y_mm=0.0),
                        shape="rect",
                        width_mm=0.2,
                        height_mm=0.2,
                        layer="top_copper",
                    )
                ],
                outline=[
                    {
                        "kind": "wire_path",
                        "layer": "51",
                        "width_mm": 0.01,
                        "points": [
                            {"x_mm": -0.2, "y_mm": 0.0},
                            {"x_mm": 0.2, "y_mm": 0.0},
                        ],
                    }
                ],
            ),
        )

    for key in sorted(supply_keys):
        symbol_id = f"SYM_PWR_{key}"
        symbol_name = f"PWR_{key}"
        device_id = f"PWR_{key}"
        symbols.setdefault(
            symbol_id,
            Symbol(
                symbol_id=symbol_id,
                name=symbol_name,
                pins=[SymbolPin(pin_number="1", pin_name=key, at=Point(x_mm=0.0, y_mm=0.0))],
                graphics=[
                    {"kind": "wire", "x1_mm": 0.0, "y1_mm": 0.0, "x2_mm": 0.0, "y2_mm": 2.54},
                    {"kind": "wire", "x1_mm": -1.27, "y1_mm": 2.54, "x2_mm": 1.27, "y2_mm": 2.54},
                ],
            ),
        )
        devices.setdefault(
            device_id,
            Device(
                device_id=device_id,
                name=device_id,
                symbol_id=symbol_id,
                package_id=supply_package_id,
                pin_pad_map={key: "1"},
            ),
        )

    root = ET.Element("eagle", {"version": "9.6.2"})
    drawing = ET.SubElement(root, "drawing")

    settings = ET.SubElement(drawing, "settings")
    ET.SubElement(settings, "setting", {"alwaysvectorfont": "no"})
    ET.SubElement(settings, "setting", {"verticaltext": "up"})

    ET.SubElement(
        drawing,
        "grid",
        {
            "distance": "0.1",
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
        (51, "tDocu", 7, 1),
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

    library = ET.SubElement(drawing, "library")
    packages_el = ET.SubElement(library, "packages")
    symbols_el = ET.SubElement(library, "symbols")
    devicesets_el = ET.SubElement(library, "devicesets")

    package_name_map: dict[str, str] = {}
    symbol_name_map: dict[str, str] = {}
    used_package_names: set[str] = set()
    used_symbol_names: set[str] = set()
    used_deviceset_names: set[str] = set()

    for package in packages.values():
        pkg_name = _allocate_unique_name(
            _sanitize_name(package.name or package.package_id),
            used_package_names,
        )
        package_name_map[package.package_id] = pkg_name
        package_el = ET.SubElement(packages_el, "package", {"name": pkg_name})

        for pad in package.pads:
            pad_name = str(pad.pad_number or "1")
            x = _fmt_mm(pad.at.x_mm)
            y = _fmt_mm(pad.at.y_mm)
            shape = str(pad.shape or "rect").lower()
            if pad.drill_mm is not None and pad.drill_mm > 0:
                attrs = _through_hole_pad_attrs(
                    pad_name=pad_name,
                    x_text=x,
                    y_text=y,
                    shape=shape,
                    width_mm=pad.width_mm,
                    height_mm=pad.height_mm,
                    drill_mm=pad.drill_mm,
                    rotation_deg=float(pad.rotation_deg),
                )
                ET.SubElement(package_el, "pad", attrs)
            else:
                layer = _smd_layer_number(str(pad.layer))
                smd_attrs = {
                    "name": pad_name,
                    "x": x,
                    "y": y,
                    "dx": _fmt_mm(max(pad.width_mm, 0.15)),
                    "dy": _fmt_mm(max(pad.height_mm, 0.15)),
                    "layer": layer,
                    "roundness": "0" if shape == "rect" else "50",
                }
                rot_text = _rotation_attr(float(pad.rotation_deg))
                if rot_text is not None:
                    smd_attrs["rot"] = rot_text
                ET.SubElement(package_el, "smd", smd_attrs)

        _emit_package_outline(package_el, package)

    for symbol in symbols.values():
        sym_name = _allocate_unique_name(
            _sanitize_name(symbol.name or symbol.symbol_id),
            used_symbol_names,
        )
        symbol_name_map[symbol.symbol_id] = sym_name
        symbol_el = ET.SubElement(symbols_el, "symbol", {"name": sym_name})

        pins = symbol.pins or []
        if not pins:
            pins = []
        pin_positions: list[tuple[float, float]] = []
        for idx, pin in enumerate(pins, start=1):
            px = pin.at.x_mm if pin.at else float(idx * 2.54)
            py = pin.at.y_mm if pin.at else 0.0
            angle = "180" if px > 0 else "0"
            pin_name = str(pin.pin_name or pin.pin_number or idx)
            direction = (
                "sup"
                if str(symbol.symbol_id or "").upper().startswith("SYM_PWR_")
                else _pin_direction(pin_name)
            )
            ET.SubElement(
                symbol_el,
                "pin",
                {
                    "name": pin_name,
                    "x": _fmt_mm(px),
                    "y": _fmt_mm(py),
                    "visible": "pad",
                    "length": "short",
                    "direction": direction,
                    "rot": f"R{angle}",
                },
            )
            pin_positions.append((float(px), float(py)))

        graphic_wires = [item for item in (symbol.graphics or []) if item.get("kind") == "wire"]
        if graphic_wires:
            for item in graphic_wires:
                ET.SubElement(
                    symbol_el,
                    "wire",
                    {
                        "x1": _fmt_mm(float(item.get("x1_mm", 0.0))),
                        "y1": _fmt_mm(float(item.get("y1_mm", 0.0))),
                        "x2": _fmt_mm(float(item.get("x2_mm", 0.0))),
                        "y2": _fmt_mm(float(item.get("y2_mm", 0.0))),
                        "width": "0.01",
                        "layer": "94",
                    },
                )
        else:
            ET.SubElement(symbol_el, "wire", {"x1": "-0.1", "y1": "-0.1", "x2": "0.1", "y2": "-0.1", "width": "0.01", "layer": "94"})
            ET.SubElement(symbol_el, "wire", {"x1": "0.1", "y1": "-0.1", "x2": "0.1", "y2": "0.1", "width": "0.01", "layer": "94"})
            ET.SubElement(symbol_el, "wire", {"x1": "0.1", "y1": "0.1", "x2": "-0.1", "y2": "0.1", "width": "0.01", "layer": "94"})
            ET.SubElement(symbol_el, "wire", {"x1": "-0.1", "y1": "0.1", "x2": "-0.1", "y2": "-0.1", "width": "0.01", "layer": "94"})

        if pin_positions:
            min_x = min(pos[0] for pos in pin_positions)
            min_y = min(pos[1] for pos in pin_positions)
            max_y = max(pos[1] for pos in pin_positions)
            name_x = min_x - 2.54
            name_y = max_y + 1.27
            value_x = min_x - 2.54
            value_y = min_y - 1.27
        else:
            name_x, name_y = (-2.54, 1.27)
            value_x, value_y = (-2.54, -1.27)

        ET.SubElement(
            symbol_el,
            "text",
            {
                "x": _fmt_mm(name_x),
                "y": _fmt_mm(name_y),
                "size": "1.27",
                "layer": "95",
            },
        ).text = ">NAME"
        ET.SubElement(
            symbol_el,
            "text",
            {
                "x": _fmt_mm(value_x),
                "y": _fmt_mm(value_y),
                "size": "1.27",
                "layer": "96",
            },
        ).text = ">VALUE"

    for device in devices.values():
        ds_name = _allocate_unique_name(
            _sanitize_name(device.device_id),
            used_deviceset_names,
        )
        symbol_name = symbol_name_map.get(device.symbol_id, _sanitize_name(device.symbol_id))
        package_key = str(device.package_id or "")
        package_name = (
            package_name_map.get(package_key, _sanitize_name(package_key))
            if package_key
            else ""
        )

        deviceset_el = ET.SubElement(
            devicesets_el,
            "deviceset",
            {"name": ds_name, "prefix": _guess_prefix(device.device_id)},
        )
        gates_el = ET.SubElement(deviceset_el, "gates")
        ET.SubElement(gates_el, "gate", {"name": "G$1", "symbol": symbol_name, "x": "0", "y": "0"})

        devices_el = ET.SubElement(deviceset_el, "devices")
        device_el = ET.SubElement(devices_el, "device", {"name": "", "package": package_name})

        if package_name and device.pin_pad_map:
            connects_el = ET.SubElement(device_el, "connects")
            for pin, pad in sorted(device.pin_pad_map.items()):
                ET.SubElement(
                    connects_el,
                    "connect",
                    {"gate": "G$1", "pin": str(pin), "pad": str(pad)},
                )

        technologies_el = ET.SubElement(device_el, "technologies")
        ET.SubElement(technologies_el, "technology", {"name": ""})

    xml_bytes = ET.tostring(root, encoding="utf-8")
    pretty = minidom.parseString(xml_bytes).toprettyxml(indent="  ", encoding="utf-8")
    library_path.write_bytes(pretty)
    return library_path


def _fmt_mm(mm: float) -> str:
    return f"{float(mm):.6f}".rstrip("0").rstrip(".") or "0"


def _through_hole_pad_attrs(
    pad_name: str,
    x_text: str,
    y_text: str,
    shape: str,
    width_mm: float,
    height_mm: float,
    drill_mm: float,
    rotation_deg: float,
) -> dict[str, str]:
    # Prefer conservative annular rings for elongated/oval pads. Using the larger copper dimension
    # as round diameter can produce oversized pads in Fusion/EAGLE.
    minor_mm = max(min(float(width_mm), float(height_mm)), 0.2)
    diameter_mm = max(minor_mm, float(drill_mm) * 1.6)

    attrs: dict[str, str] = {
        "name": pad_name,
        "x": x_text,
        "y": y_text,
        "drill": _fmt_mm(drill_mm),
        "diameter": _fmt_mm(diameter_mm),
    }

    shape_key = shape.lower()
    rot_total = float(rotation_deg or 0.0)
    if shape_key in {"square", "octagon"}:
        attrs["shape"] = shape_key
        rot_text = _rotation_attr(rot_total)
        if rot_text is not None:
            attrs["rot"] = rot_text
        return attrs

    if shape_key in {"oval", "ellipse"}:
        if abs(float(height_mm) - float(width_mm)) <= 1e-6:
            attrs["shape"] = "round"
            return attrs

        attrs["shape"] = "long"
        if float(height_mm) > float(width_mm):
            rot_total += 90.0
        rot_text = _rotation_attr(rot_total)
        if rot_text is not None:
            attrs["rot"] = rot_text
        return attrs

    attrs["shape"] = "round"
    return attrs


def _rotation_attr(rotation_deg: float) -> str | None:
    angle = int(round(float(rotation_deg or 0.0))) % 360
    if angle == 0:
        return None
    return f"R{angle}"


def _emit_package_outline(package_el: ET.Element, package: Package) -> None:
    side = _default_package_side(package)
    package_name_norm = _canon_token(package.name or package.package_id)
    name_layer = "25" if side == "top" else "26"
    value_layer = "27" if side == "top" else "28"
    name_x, name_y, value_x, value_y = _default_name_value_positions(package)
    extra_value_text_y = value_y - 1.8

    for item in package.outline:
        kind = str(item.get("kind", "")).strip().lower()
        if kind == "wire_path":
            layer = _package_wire_layer(str(item.get("layer", "")))
            if layer is None:
                continue
            width = max(float(item.get("width_mm", 0.01) or 0.01), 0.01)
            points = item.get("points")
            if not isinstance(points, list) or len(points) < 2:
                continue
            cleaned = _clean_outline_points(points)
            if len(cleaned) < 2:
                continue
            for idx in range(len(cleaned) - 1):
                p1 = cleaned[idx]
                p2 = cleaned[idx + 1]
                ET.SubElement(
                    package_el,
                    "wire",
                    {
                        "x1": _fmt_mm(float(p1.get("x_mm", 0.0))),
                        "y1": _fmt_mm(float(p1.get("y_mm", 0.0))),
                        "x2": _fmt_mm(float(p2.get("x_mm", 0.0))),
                        "y2": _fmt_mm(float(p2.get("y_mm", 0.0))),
                        "width": _fmt_mm(width),
                        "layer": layer,
                    },
                )
            continue

        if kind == "text":
            raw_text = str(item.get("text", "") or "").strip()
            if not raw_text:
                continue
            text_token = raw_text.upper()
            if text_token in {"DESIGNATOR", "REF", "REFERENCE"}:
                raw_text = ">NAME"
                text_token = ">NAME"
            elif text_token in {"VALUE", "VAL"}:
                raw_text = ">VALUE"
                text_token = ">VALUE"

            is_part_name = _is_package_part_name_text(raw_text, package_name_norm)
            if text_token in {">NAME", ">VALUE"}:
                # Always place NAME/VALUE in deterministic safe locations.
                continue

            layer = _package_text_layer(
                source_layer=str(item.get("layer", "")),
                text_token=text_token,
                default_side=side,
                is_part_name=is_part_name,
            )
            if layer is None:
                continue
            x = float(item.get("x_mm", 0.0))
            y = float(item.get("y_mm", 0.0))
            if is_part_name:
                layer = value_layer
                x, y = _safe_text_point(
                    package,
                    value_x,
                    extra_value_text_y,
                    fallback_points=[
                        (value_x, extra_value_text_y),
                        (value_x, value_y),
                        (name_x, name_y),
                    ],
                )
                extra_value_text_y -= 1.5
            else:
                x, y = _safe_text_point(
                    package,
                    x,
                    y,
                    fallback_points=[
                        (name_x, name_y + 1.8),
                        (value_x, value_y - 1.8),
                    ],
                )

            attrs = {
                "x": _fmt_mm(x),
                "y": _fmt_mm(y),
                "size": _fmt_mm(max(float(item.get("size_mm", 1.0) or 1.0), 0.6)),
                "layer": layer,
            }
            rot = _rotation_attr(float(item.get("rotation_deg", 0.0) or 0.0))
            if rot is not None:
                attrs["rot"] = rot
            text_el = ET.SubElement(package_el, "text", attrs)
            text_el.text = raw_text

    ET.SubElement(
        package_el,
        "text",
        {
            "x": _fmt_mm(name_x),
            "y": _fmt_mm(name_y),
            "size": "1.27",
            "layer": name_layer,
        },
    ).text = ">NAME"
    ET.SubElement(
        package_el,
        "text",
        {
            "x": _fmt_mm(value_x),
            "y": _fmt_mm(value_y),
            "size": "1.27",
            "layer": value_layer,
        },
    ).text = ">VALUE"


def _emit_pad_number_labels(package_el: ET.Element, package: Package) -> None:
    if not package.pads:
        return

    centroid_x = sum(float(pad.at.x_mm) for pad in package.pads) / float(len(package.pads))
    centroid_y = sum(float(pad.at.y_mm) for pad in package.pads) / float(len(package.pads))

    for pad in package.pads:
        label = str(pad.pad_number or "").strip()
        if not label:
            continue

        px = float(pad.at.x_mm)
        py = float(pad.at.y_mm)
        vx = px - centroid_x
        vy = py - centroid_y
        mag = (vx * vx + vy * vy) ** 0.5
        if mag < 1e-6:
            vx, vy = 0.0, 1.0
            mag = 1.0

        radial_offset = max(max(float(pad.width_mm), float(pad.height_mm)) * 0.5 + 0.35, 0.55)
        lx = px + (vx / mag) * radial_offset
        ly = py + (vy / mag) * radial_offset
        if _text_overlaps_pad(package, lx, ly, clearance_mm=0.15):
            lx = px - (vx / mag) * radial_offset
            ly = py - (vy / mag) * radial_offset

        ET.SubElement(
            package_el,
            "text",
            {
                "x": _fmt_mm(lx),
                "y": _fmt_mm(ly),
                "size": _fmt_mm(_pad_label_size_mm(pad.width_mm, pad.height_mm)),
                "layer": "51",
                "align": "center",
            },
        ).text = label


def _pad_label_size_mm(width_mm: float, height_mm: float) -> float:
    shortest = max(min(float(width_mm), float(height_mm)), 0.2)
    return max(0.6, min(1.0, shortest * 0.8))


def _clean_outline_points(points: list[dict[str, float]]) -> list[dict[str, float]]:
    out: list[dict[str, float]] = []
    for point in points:
        if not isinstance(point, dict):
            continue
        x = float(point.get("x_mm", 0.0))
        y = float(point.get("y_mm", 0.0))
        if out:
            px = float(out[-1].get("x_mm", 0.0))
            py = float(out[-1].get("y_mm", 0.0))
            if abs(x - px) < 1e-6 and abs(y - py) < 1e-6:
                continue
        out.append({"x_mm": x, "y_mm": y})
    return out


def _default_name_value_positions(package: Package) -> tuple[float, float, float, float]:
    min_x, min_y, max_x, max_y = _package_bounds(package)
    span_y = max(max_y - min_y, 0.0)
    top_margin = max(1.2, span_y * 0.25 + 0.8)
    bottom_margin = max(1.4, span_y * 0.30 + 0.8)
    x_anchor = min_x
    return (x_anchor, max_y + top_margin, x_anchor, min_y - bottom_margin)


def _safe_text_point(
    package: Package,
    x: float,
    y: float,
    fallback_points: list[tuple[float, float]],
) -> tuple[float, float]:
    if not _text_overlaps_pad(package, x, y):
        return x, y
    for fx, fy in fallback_points:
        if not _text_overlaps_pad(package, fx, fy):
            return fx, fy
    if fallback_points:
        return fallback_points[0]
    return x, y


def _text_overlaps_pad(package: Package, x: float, y: float, clearance_mm: float = 0.4) -> bool:
    for pad in package.pads:
        half_w = max(float(pad.width_mm), 0.0) / 2.0 + clearance_mm
        half_h = max(float(pad.height_mm), 0.0) / 2.0 + clearance_mm
        if (
            float(pad.at.x_mm) - half_w <= x <= float(pad.at.x_mm) + half_w
            and float(pad.at.y_mm) - half_h <= y <= float(pad.at.y_mm) + half_h
        ):
            return True
    return False


def _default_package_side(package: Package) -> str:
    top = 0
    bottom = 0
    for pad in package.pads:
        key = str(pad.layer or "").strip().lower()
        if key in {"2", "16", "bottom", "bottom_copper", "bottomlayer"}:
            bottom += 1
        else:
            top += 1
    return "bottom" if bottom > top else "top"


def _package_wire_layer(source_layer: str) -> str | None:
    key = str(source_layer or "").strip().lower()
    if key in {"3", "top_silkscreen", "topsilkscreen", "topsilklayer"}:
        return "21"
    if key in {"4", "bottom_silkscreen", "bottomsilkscreen", "bottomsilklayer"}:
        return "22"
    if key in {"49", "component_marking", "componentmarkinglayer"}:
        return "21"
    if key in {"50"}:
        # EasyEDA Pro footprints often place body detail on layer 50.
        # Keep these vectors on documentation rather than dropping them.
        return "51"
    if key in {"48", "component_shape", "componentshapelayer", "13", "documentation"}:
        return "51"
    return None


def _package_text_layer(
    source_layer: str,
    text_token: str,
    default_side: str,
    is_part_name: bool = False,
) -> str | None:
    key = str(source_layer or "").strip().lower()
    bottom = key in {"4", "bottom_silkscreen", "bottomsilkscreen", "bottomsilklayer"}
    side = "bottom" if bottom else default_side

    if text_token == ">NAME":
        return "26" if side == "bottom" else "25"
    if text_token == ">VALUE":
        return "28" if side == "bottom" else "27"
    if is_part_name:
        return "28" if side == "bottom" else "27"

    mapped = _package_wire_layer(source_layer)
    return mapped if mapped is not None else "21"


def _is_package_part_name_text(text: str, package_name_norm: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    if raw.startswith(">"):
        return False
    if _looks_like_designator(raw):
        return False

    token = _canon_token(text)
    if not token or not package_name_norm:
        return _looks_like_part_number(raw)
    if token in {"NAME", "VALUE"}:
        return False
    if token == package_name_norm:
        return True
    return _looks_like_part_number(raw)


def _canon_token(value: str) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _looks_like_designator(text: str) -> bool:
    token = str(text or "").strip().upper()
    # Common designators like R1, C12, U3, TP4, J1.
    match = re.fullmatch(r"([A-Z]{1,3})(\d{1,4})", token)
    if not match:
        return False
    digits = match.group(2)
    # Package codes such as R0603/C0805 are not designators.
    if len(digits) >= 3 and digits.startswith("0"):
        return False
    return True


def _looks_like_part_number(text: str) -> bool:
    token = str(text or "").strip().upper()
    if len(token) < 5:
        return False
    has_letter = any(ch.isalpha() for ch in token)
    has_digit = any(ch.isdigit() for ch in token)
    if not (has_letter and has_digit):
        return False
    return bool(re.fullmatch(r"[A-Z0-9._+\-/]+", token))


def _package_bounds(package: Package) -> tuple[float, float, float, float]:
    if not package.pads:
        return (0.0, 0.0, 0.0, 0.0)
    min_x = min(pad.at.x_mm - pad.width_mm / 2.0 for pad in package.pads)
    max_x = max(pad.at.x_mm + pad.width_mm / 2.0 for pad in package.pads)
    min_y = min(pad.at.y_mm - pad.height_mm / 2.0 for pad in package.pads)
    max_y = max(pad.at.y_mm + pad.height_mm / 2.0 for pad in package.pads)
    return (min_x, min_y, max_x, max_y)


def _project_supply_keys(project: Project | None) -> set[str]:
    if project is None:
        return set()
    keys: set[str] = set()
    for net in project.nets:
        normalized = _normalize_power_key(net.name)
        if normalized:
            keys.add(normalized)
    return keys


def _normalize_power_key(net_name: str) -> str | None:
    token = "".join(ch for ch in str(net_name or "").upper() if ch.isalnum())
    if not token:
        return None
    direct = {
        "GND": "GND",
        "AGND": "AGND",
        "DGND": "DGND",
        "PGND": "PGND",
        "EARTH": "EARTH",
        "CHASSIS": "CHASSIS",
        "VCC": "VCC",
        "VDD": "VDD",
        "VSS": "VSS",
        "VBAT": "VBAT",
        "VIN": "VIN",
        "AVDD": "AVDD",
        "DVDD": "DVDD",
        "3V3": "3V3",
        "33V": "3V3",
        "V33": "3V3",
        "5V": "5V",
        "5V0": "5V",
        "V5": "5V",
        "12V": "12V",
        "12V0": "12V",
        "V12": "12V",
    }
    return direct.get(token)


def _sanitize_name(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(name or ""))
    cleaned = cleaned.strip("_")
    return cleaned or "GEN"


def _guess_prefix(name: str) -> str:
    token = str(name or "").strip().upper()
    if token.startswith("DEV_"):
        token = token[4:]
    token = token.lstrip("_")

    match = re.match(r"([A-Z]+)", token)
    if match:
        return match.group(1)
    return "U"


def _allocate_unique_name(base_name: str, used: set[str]) -> str:
    base = _sanitize_name(base_name)
    candidate = base
    idx = 2
    while candidate in used:
        candidate = f"{base}_{idx}"
        idx += 1
    used.add(candidate)
    return candidate


def _smd_layer_number(layer_name: str) -> str:
    key = str(layer_name or "").strip().lower()
    if key in {"2", "16", "bottom", "bottom_copper", "bottomlayer"}:
        return "16"
    return "1"


def _pin_direction(pin_name: str) -> str:
    token = "".join(ch for ch in str(pin_name or "").upper() if ch.isalnum())
    if token in {"GND", "AGND", "DGND", "PGND", "SGND", "VCC", "VDD", "VSS", "VIN", "VOUT", "3V3", "5V", "5V0", "12V", "24V"}:
        return "pwr"
    return "pas"
