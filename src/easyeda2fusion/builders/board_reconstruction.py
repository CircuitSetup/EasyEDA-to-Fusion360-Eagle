from __future__ import annotations

import math

from easyeda2fusion.builders.board_layers import (
    is_copper_layer_num as _shared_is_copper_layer_num,
    is_keepout_layer_num as _shared_is_keepout_layer_num,
    layer_number as _shared_layer_number,
)
from easyeda2fusion.builders.component_identity import (
    build_refdes_map as _shared_build_refdes_map,
    component_instance_key as _shared_component_instance_key,
    resolve_component_refdes as _shared_resolve_component_refdes,
    sanitize_refdes as _shared_sanitize_refdes,
)
from easyeda2fusion.builders.net_aliases import (
    build_track_net_aliases as _shared_build_track_net_aliases,
    canonical_net_name as _shared_canonical_net_name,
    project_track_net_aliases as _shared_project_track_net_aliases,
)
from easyeda2fusion.builders.package_utils import (
    canonicalize_two_pin_quarter_turn as _shared_canonicalize_two_pin_quarter_turn,
    component_is_resistor as _shared_component_is_resistor,
    is_adjustable_resistor_package as _shared_is_adjustable_resistor_package,
    package_lookup as _shared_package_lookup,
    package_pin_count as _shared_package_pin_count,
    resolve_component_package as _shared_resolve_component_package,
    valid_pins_by_ref as _shared_valid_pins_by_ref,
)
from easyeda2fusion.model import Package, Project, Severity, Side, SourceFormat, project_event

_DEFAULT_POLYGON_WIDTH_MM = 0.254


class BoardReconstructionBuilder:
    """Builds EAGLE script command lines for board reconstruction."""

    def build_commands(self, project: Project) -> list[str]:
        lines: list[str] = [
            "GRID MM 0.05 ON;",
            "SET WIRE_BEND 2;",
            "SET SPIN 1;",
        ]

        board = project.board
        if board is None:
            return lines
        net_alias = _project_track_net_aliases(project)

        refdes_map = _build_refdes_map(project)
        package_lookup = _package_lookup(project)
        valid_pins_by_ref = _valid_pins_by_ref(project)
        placed_refs: set[str] = set()
        skipped_no_device: list[str] = []
        placed_instance_records: list[dict[str, str]] = []
        for ordinal, component in enumerate(project.components, start=1):
            if not str(component.device_id or "").strip():
                skipped_no_device.append(str(component.refdes or "").strip())
                continue
            safe_refdes = _resolve_component_refdes(component, refdes_map)
            placed_refs.add(safe_refdes)
            placed_instance_records.append(
                {
                    "source_refdes": str(component.refdes or ""),
                    "source_instance_id": str(getattr(component, "source_instance_id", "") or ""),
                    "source_component_key": _component_refdes_key(component, ordinal),
                    "emitted_refdes": safe_refdes,
                }
            )
            package_obj = _resolve_component_package(component, package_lookup)
            effective_rotation_deg = _resolved_board_component_rotation_deg(
                component=component,
                source_format=project.source_format,
                package=package_obj,
            )
            lines.append(
                f"ROTATE ={_orientation_token(effective_rotation_deg, component.side)} {_quote_token(safe_refdes)};"
            )
            move_x, move_y = _component_move_point_mm(component, effective_rotation_deg=effective_rotation_deg)
            lines.append(f"MOVE {safe_refdes} ({move_x:.4f} {move_y:.4f});")
        _record_skipped_components_without_device(project, skipped_no_device)
        project.metadata["board_instance_refdes_map"] = placed_instance_records
        project.metadata["board_skipped_components_without_device"] = sorted({item for item in skipped_no_device if item})

        # Assign pads to signals first so board imports retain logical connectivity.
        signal_pairs_by_name: dict[str, list[str]] = {}
        seen_pairs_by_name: dict[str, set[str]] = {}
        for net in project.nets:
            if not net.nodes:
                continue
            canonical_net_name = _canonical_net_name(net.name, net_alias)
            if not canonical_net_name:
                continue
            for node in net.nodes:
                ref = refdes_map.get(node.refdes, _sanitize_refdes(node.refdes))
                if ref not in placed_refs:
                    continue
                pin = str(node.pin).replace("'", "")
                if not pin:
                    continue
                valid_pins = valid_pins_by_ref.get(ref, set())
                if valid_pins and pin not in valid_pins:
                    continue
                pair = f"{ref} {pin}"
                seen_pairs = seen_pairs_by_name.setdefault(canonical_net_name, set())
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                signal_pairs_by_name.setdefault(canonical_net_name, []).append(pair)
        # In linked schematic+board projects, SIGNAL editing belongs to schematic.
        # Emitting SIGNAL in board context triggers "This action must be performed
        # in the schematic" and interrupts script execution.
        emit_signal_commands = not bool(project.sheets)
        if emit_signal_commands:
            for signal_name, pairs in signal_pairs_by_name.items():
                if len(pairs) >= 2:
                    lines.append(f"SIGNAL {_quote_token(signal_name)} {' '.join(pairs)};")

        if board.outline:
            lines.extend(_emit_remove_existing_board_outline(board))

        for outline in board.outline:
            lines.extend(_emit_region_wires(outline.layer, outline.points, width_mm=0.0, close=True))
        for cutout in board.cutouts:
            lines.extend(_emit_region_wires(cutout.layer or "46", cutout.points, width_mm=0.0, close=True))
        for keepout in board.keepouts:
            lines.extend(_emit_keepout_polygon(keepout.layer or "41", keepout.points))
        for region in board.regions:
            if _is_copper_polygon_region(region.layer, region.net, region.points):
                region_net = _canonical_net_name(region.net, net_alias)
                lines.extend(_emit_copper_polygon(region.layer, region_net, region.points))
            elif _is_keepout_region(region.layer, region.points):
                lines.extend(_emit_keepout_polygon(region.layer, region.points))
            else:
                lines.extend(_emit_region_wires(region.layer, region.points, width_mm=0.0, close=True))

        current_drill_mm: float | None = None
        for free_pad in _standalone_board_pads(project, board.pads):
            if free_pad.drill_mm is None or free_pad.drill_mm <= 0:
                continue
            if current_drill_mm is None or abs(current_drill_mm - free_pad.drill_mm) > 1e-6:
                lines.append(f"CHANGE DRILL {free_pad.drill_mm:.4f};")
                current_drill_mm = free_pad.drill_mm
            via_shape = _via_shape_from_pad_shape(free_pad.shape)
            diameter = max(free_pad.width_mm, free_pad.height_mm, free_pad.drill_mm * 1.2)
            net_name = _canonical_net_name(free_pad.net, net_alias)
            if net_name:
                lines.append(
                    f"VIA {_quote_token(net_name)} {diameter:.4f} {via_shape} ({free_pad.at.x_mm:.4f} {free_pad.at.y_mm:.4f});"
                )
            else:
                lines.append(
                    f"VIA {diameter:.4f} {via_shape} ({free_pad.at.x_mm:.4f} {free_pad.at.y_mm:.4f});"
                )

        current_layer = ""
        emitted_track_keys: set[tuple[str, str, str, str, str, str]] = set()
        for track in board.tracks:
            if (
                abs(track.start.x_mm - track.end.x_mm) < 1e-6
                and abs(track.start.y_mm - track.end.y_mm) < 1e-6
            ):
                continue
            layer_number = _layer_number(track.layer)
            if layer_number == "51":
                continue

            track_key = _track_dedupe_key(track, layer_number, net_alias)
            if track_key in emitted_track_keys:
                continue
            emitted_track_keys.add(track_key)

            layer_command = _track_layer_command_token(layer_number)
            if layer_command != current_layer:
                lines.append(f"LAYER {layer_command};")
                current_layer = layer_command
            wire_prefix = "WIRE"
            canonical_track_net = _canonical_net_name(track.net, net_alias)
            if canonical_track_net and _is_copper_layer_num(layer_number):
                # Bind copper traces to explicit signal names to avoid interactive
                # "merge N$xx" prompts during script replay.
                wire_prefix = f"WIRE {_quote_token(canonical_track_net)}"
            wire_width = _track_wire_width_for_layer(layer_number, track.width_mm)
            lines.append(
                f"{wire_prefix} {wire_width:.4f} ({track.start.x_mm:.4f} {track.start.y_mm:.4f}) ({track.end.x_mm:.4f} {track.end.y_mm:.4f});"
            )

        for via in board.vias:
            via_drill = max(float(via.drill_mm), 0.05)
            if current_drill_mm is None or abs(current_drill_mm - via_drill) > 1e-6:
                lines.append(f"CHANGE DRILL {via_drill:.4f};")
                current_drill_mm = via_drill
            net_name = _canonical_net_name(via.net, net_alias)
            if net_name:
                lines.append(
                    f"VIA {_quote_token(net_name)} {via.diameter_mm:.4f} round ({via.at.x_mm:.4f} {via.at.y_mm:.4f});"
                )
                continue
            lines.append(
                f"VIA {via.diameter_mm:.4f} round ({via.at.x_mm:.4f} {via.at.y_mm:.4f});"
            )

        for hole in board.holes:
            lines.append(f"HOLE {hole.drill_mm:.4f} ({hole.at.x_mm:.4f} {hole.at.y_mm:.4f});")

        for text in board.text:
            payload = text.text.replace("'", "")
            if payload:
                layer_num = _layer_number(text.layer)
                lines.append(f"LAYER {layer_num};")
                lines.append(f"CHANGE SIZE {max(float(text.size_mm), 0.1):.4f};")
                mirrored = bool(text.mirrored) or layer_num in {"22", "26", "28"}
                rotation = _board_text_rotation_deg(
                    source_format=project.source_format,
                    layer_num=layer_num,
                    rotation_deg=float(text.rotation_deg or 0.0),
                    y_axis_inverted=bool(project.metadata.get("y_axis_inverted", False)),
                )
                orient = f"MR{rotation}" if mirrored else f"R{rotation}"
                lines.append(
                    f"TEXT '{payload}' ({text.at.x_mm:.4f} {text.at.y_mm:.4f}) {orient};"
                )

        lines.append("RATSNEST;")
        return lines


def _build_refdes_map(project: Project) -> dict[str, str]:
    return _shared_build_refdes_map(project)


def _component_refdes_key(component, ordinal: int) -> str:
    return _shared_component_instance_key(component, ordinal)


def _resolve_component_refdes(component, refdes_map: dict[str, str]) -> str:
    return _shared_resolve_component_refdes(component, refdes_map)


def _sanitize_refdes(value: str) -> str:
    return _shared_sanitize_refdes(value)


def _quote_token(value: str) -> str:
    text = str(value or "").replace("'", "")
    return f"'{text}'"


def _layer_number(layer_name: str) -> str:
    return _shared_layer_number(layer_name)


def _emit_region_wires(layer: str, points, width_mm: float, close: bool) -> list[str]:
    cleaned = _clean_points(points, close=close)
    if len(cleaned) < 2:
        return []
    layer_num = _layer_number(str(layer))
    if layer_num == "51" and str(layer).strip().isdigit():
        # Unknown numeric layer ids are skipped to avoid corrupting board geometry.
        return []
    lines: list[str] = [f"LAYER {layer_num};"]
    count = len(cleaned) if close else len(cleaned) - 1
    for idx in range(max(0, count)):
        start = cleaned[idx]
        end = cleaned[(idx + 1) % len(cleaned)]
        wire_width = _region_wire_width_for_layer(layer_num, width_mm)
        lines.append(
            f"WIRE {wire_width:.4f} ({start.x_mm:.4f} {start.y_mm:.4f}) ({end.x_mm:.4f} {end.y_mm:.4f});"
        )
    return lines


def _emit_copper_polygon(layer: str, net_name: str, points) -> list[str]:
    cleaned = _clean_points(points, close=True)
    if len(cleaned) < 3:
        return []
    layer_num = _layer_number(str(layer))
    if not _is_copper_layer_num(layer_num):
        return []
    layer_cmd = _track_layer_command_token(layer_num)
    coords = " ".join(f"({pt.x_mm:.4f} {pt.y_mm:.4f})" for pt in cleaned)
    return [
        f"LAYER {layer_cmd};",
        f"POLYGON {_quote_token(net_name)} {_DEFAULT_POLYGON_WIDTH_MM:.4f} {coords};",
    ]


def _emit_keepout_polygon(layer: str, points) -> list[str]:
    cleaned = _clean_points(points, close=True)
    if len(cleaned) < 3:
        return _emit_region_wires(layer, points, width_mm=0.0, close=True)
    layer_num = _layer_number(str(layer))
    if not _is_keepout_layer_num(layer_num):
        return _emit_region_wires(layer, points, width_mm=0.0, close=True)
    coords = " ".join(f"({pt.x_mm:.4f} {pt.y_mm:.4f})" for pt in cleaned)
    return [
        f"LAYER {layer_num};",
        f"POLYGON {_DEFAULT_POLYGON_WIDTH_MM:.4f} {coords};",
    ]


def _emit_remove_existing_board_outline(board) -> list[str]:
    # Clear existing board-outline entities on the Dimension layer before
    # reconstructing source outline geometry to avoid duplicate outlines.
    min_x, min_y, max_x, max_y = _board_outline_clear_bounds(board)
    delete_x = min_x
    delete_y = min_y
    return [
        "DISPLAY NONE 20;",
        "LAYER 20;",
        f"GROUP ({min_x:.4f} {min_y:.4f}) ({max_x:.4f} {max_y:.4f});",
        f"DELETE (> {delete_x:.4f} {delete_y:.4f});",
        "DISPLAY ALL;",
    ]


def _board_outline_clear_bounds(board) -> tuple[float, float, float, float]:
    points = []
    for region in getattr(board, "outline", []) or []:
        for point in getattr(region, "points", []) or []:
            points.append((float(point.x_mm), float(point.y_mm)))

    if not points:
        return (-100.0, -100.0, 100.0, 100.0)

    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    min_x = min(xs)
    max_x = max(xs)
    min_y = min(ys)
    max_y = max(ys)
    padding = 10.0
    return (
        min_x - padding,
        min_y - padding,
        max_x + padding,
        max_y + padding,
    )


def _is_copper_polygon_region(layer: str, net_name: str | None, points) -> bool:
    if len(points) < 3:
        return False
    if not str(net_name or "").strip():
        return False
    layer_num = _layer_number(str(layer))
    return _is_copper_layer_num(layer_num)


def _is_copper_layer_num(layer_num: str) -> bool:
    return _shared_is_copper_layer_num(layer_num)


def _is_keepout_layer_num(layer_num: str) -> bool:
    return _shared_is_keepout_layer_num(layer_num)


def _is_keepout_region(layer: str, points) -> bool:
    if len(points) < 3:
        return False
    layer_num = _layer_number(str(layer))
    return _is_keepout_layer_num(layer_num)


def _region_wire_width_for_layer(layer_num: str, width_mm: float) -> float:
    # Emit fixed user-requested silkscreen wire width.
    if layer_num in {"21", "22"}:
        return 0.3
    return max(float(width_mm), 0.0)


def _track_wire_width_for_layer(layer_num: str, width_mm: float) -> float:
    # Emit fixed user-requested silkscreen wire width.
    if layer_num in {"21", "22"}:
        return 0.3
    return max(float(width_mm), 0.01)


def _track_layer_command_token(layer_num: str) -> str:
    if str(layer_num) == "16":
        return "cb"
    if str(layer_num) == "1":
        return "ct"
    return str(layer_num)


def _clean_points(points, close: bool) -> list:
    if not points:
        return []
    out = [points[0]]
    for point in points[1:]:
        last = out[-1]
        if abs(point.x_mm - last.x_mm) < 1e-6 and abs(point.y_mm - last.y_mm) < 1e-6:
            continue
        out.append(point)

    if close and len(out) > 1:
        first = out[0]
        last = out[-1]
        if abs(first.x_mm - last.x_mm) < 1e-6 and abs(first.y_mm - last.y_mm) < 1e-6:
            out.pop()
    return out


def _track_dedupe_key(
    track,
    layer_number: str,
    net_alias: dict[str, str],
) -> tuple[str, str, str, str, str, str]:
    sx = float(track.start.x_mm)
    sy = float(track.start.y_mm)
    ex = float(track.end.x_mm)
    ey = float(track.end.y_mm)
    if (ex < sx) or (abs(ex - sx) < 1e-9 and ey < sy):
        sx, sy, ex, ey = ex, ey, sx, sy

    return (
        layer_number,
        _canonical_net_name(track.net, net_alias),
        f"{_track_wire_width_for_layer(layer_number, track.width_mm):.4f}",
        f"{sx:.4f}",
        f"{sy:.4f}",
        f"{ex:.4f},{ey:.4f}",
    )


def _valid_pins_by_ref(project: Project) -> dict[str, set[str]]:
    return _shared_valid_pins_by_ref(project)


def _orientation_token(rotation_deg: float, side: Side) -> str:
    angle = int(round(float(rotation_deg or 0.0))) % 360
    if side == Side.BOTTOM:
        return f"MR{angle}"
    return f"R{angle}"


def _component_rotation_with_external_offset(component) -> float:
    attrs = getattr(component, "attributes", {}) or {}
    try:
        delta = float(attrs.get("_external_rotation_offset_deg", 0.0))
    except Exception:
        delta = 0.0
    return float(component.rotation_deg or 0.0) + delta


def _resolved_board_component_rotation_deg(
    component,
    source_format: SourceFormat,
    package: Package | None,
) -> float:
    resolved = _component_rotation_with_external_offset(component)
    if source_format == SourceFormat.EASYEDA_PRO and _component_is_resistor(component):
        resolved = -resolved
        if package is not None and _package_pin_count(package) == 2:
            resolved = _canonicalize_two_pin_quarter_turn(resolved)
        if package is not None and _is_adjustable_resistor_package(package):
            snapped = int(round(float(resolved or 0.0))) % 360
            if snapped == 180:
                resolved = 90.0
    return resolved


def _component_move_point_mm(component, effective_rotation_deg: float | None = None) -> tuple[float, float]:
    x = float(component.at.x_mm)
    y = float(component.at.y_mm)
    attrs = getattr(component, "attributes", {}) or {}
    try:
        dx = float(attrs.get("_external_origin_offset_x_mm", 0.0))
        dy = float(attrs.get("_external_origin_offset_y_mm", 0.0))
    except Exception:
        dx = 0.0
        dy = 0.0

    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return x, y

    if component.side == Side.BOTTOM:
        dx = -dx

    rotation_for_offset_deg = (
        float(component.rotation_deg or 0.0)
        if effective_rotation_deg is None
        else float(effective_rotation_deg)
    )
    # External library transforms encode pad mapping as:
    #   source_local = rotate(external_local, external_rot) + offset
    # Move offset therefore lives in the pre-external-rotation local frame and
    # must be rotated by the base instance rotation, not by the fully effective
    # rotation that already includes external_rot.
    if effective_rotation_deg is not None:
        try:
            external_rot = float(attrs.get("_external_rotation_offset_deg", 0.0))
        except Exception:
            external_rot = 0.0
        rotation_for_offset_deg = float(effective_rotation_deg) - external_rot

    angle = math.radians(rotation_for_offset_deg)
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    ox = dx * cos_a - dy * sin_a
    oy = dx * sin_a + dy * cos_a
    return x + ox, y + oy


def _board_text_rotation_deg(
    source_format: SourceFormat,
    layer_num: str,
    rotation_deg: float,
    y_axis_inverted: bool = False,
) -> int:
    angle = int(round(float(rotation_deg or 0.0))) % 360
    if y_axis_inverted and source_format == SourceFormat.EASYEDA_STD:
        # Legacy Standard shape-string board text already carries usable screen
        # orientation after coordinate normalization; preserve authored angle.
        return angle
    if source_format == SourceFormat.EASYEDA_PRO and layer_num in {"21", "22"}:
        # EasyEDA Pro board STRING rotation is clockwise-positive on silkscreen.
        # EAGLE rotation is counter-clockwise-positive.
        return (-angle) % 360
    return angle


def _package_lookup(project: Project) -> dict[str, Package]:
    return _shared_package_lookup(project)


def _resolve_component_package(component, package_lookup: dict[str, Package]) -> Package | None:
    return _shared_resolve_component_package(component, package_lookup)


def _package_pin_count(package: Package) -> int:
    return _shared_package_pin_count(package)


def _component_is_resistor(component) -> bool:
    return _shared_component_is_resistor(component)


def _canonicalize_two_pin_quarter_turn(rotation_deg: float) -> float:
    return _shared_canonicalize_two_pin_quarter_turn(rotation_deg)


def _is_adjustable_resistor_package(package: Package) -> bool:
    return _shared_is_adjustable_resistor_package(package)


def _norm_pkg_token(value: str) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _canonical_net_name(name: str | None, net_alias: dict[str, str]) -> str:
    return _shared_canonical_net_name(name, net_alias)


def _build_track_net_aliases(tracks, vias=None) -> dict[str, str]:
    return _shared_build_track_net_aliases(tracks, vias)


def _project_track_net_aliases(project: Project) -> dict[str, str]:
    return _shared_project_track_net_aliases(project)


def _pick_canonical_net_name(names: list[str]) -> str:
    cleaned = [str(name or "").strip() for name in names if str(name or "").strip()]
    if not cleaned:
        return ""

    def sort_key(value: str) -> tuple[int, int, str]:
        upper = value.upper()
        anonymous = 1 if upper.startswith("N$") else 0
        return (anonymous, len(value), upper)

    return sorted(cleaned, key=sort_key)[0]


def _segments_touch_or_overlap(
    seg_a: tuple[tuple[float, float], tuple[float, float]],
    seg_b: tuple[tuple[float, float], tuple[float, float]],
    eps: float = 1e-6,
) -> bool:
    ax1, ay1 = seg_a[0]
    ax2, ay2 = seg_a[1]
    bx1, by1 = seg_b[0]
    bx2, by2 = seg_b[1]

    if (
        abs(ax1 - ax2) < eps
        and abs(ay1 - ay2) < eps
        or abs(bx1 - bx2) < eps
        and abs(by1 - by2) < eps
    ):
        return False

    return _line_segments_intersect(
        (ax1, ay1),
        (ax2, ay2),
        (bx1, by1),
        (bx2, by2),
        eps=eps,
    )


def _point_on_segment(
    point: tuple[float, float],
    segment: tuple[tuple[float, float], tuple[float, float]],
    eps: float = 1e-3,
) -> bool:
    px, py = point
    (x1, y1), (x2, y2) = segment

    if (
        px < min(x1, x2) - eps
        or px > max(x1, x2) + eps
        or py < min(y1, y2) - eps
        or py > max(y1, y2) + eps
    ):
        return False

    dx = x2 - x1
    dy = y2 - y1
    if abs(dx) < eps and abs(dy) < eps:
        return abs(px - x1) <= eps and abs(py - y1) <= eps

    cross = (px - x1) * dy - (py - y1) * dx
    if abs(cross) > eps * max(abs(dx), abs(dy), 1.0):
        return False

    dot = (px - x1) * dx + (py - y1) * dy
    if dot < -eps:
        return False
    length_sq = dx * dx + dy * dy
    if dot > length_sq + eps:
        return False
    return True


def _line_segments_intersect(
    a1: tuple[float, float],
    a2: tuple[float, float],
    b1: tuple[float, float],
    b2: tuple[float, float],
    eps: float = 1e-6,
) -> bool:
    def orient(p, q, r) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    def on_segment(p, q, r) -> bool:
        return (
            min(p[0], r[0]) - eps <= q[0] <= max(p[0], r[0]) + eps
            and min(p[1], r[1]) - eps <= q[1] <= max(p[1], r[1]) + eps
        )

    o1 = orient(a1, a2, b1)
    o2 = orient(a1, a2, b2)
    o3 = orient(b1, b2, a1)
    o4 = orient(b1, b2, a2)

    if (o1 > eps and o2 < -eps or o1 < -eps and o2 > eps) and (
        o3 > eps and o4 < -eps or o3 < -eps and o4 > eps
    ):
        return True

    if abs(o1) <= eps and on_segment(a1, b1, a2):
        return True
    if abs(o2) <= eps and on_segment(a1, b2, a2):
        return True
    if abs(o3) <= eps and on_segment(b1, a1, b2):
        return True
    if abs(o4) <= eps and on_segment(b1, a2, b2):
        return True
    return False


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, value: str) -> None:
        if value not in self.parent:
            self.parent[value] = value

    def find(self, value: str) -> str:
        self.add(value)
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        l_root = self.find(left)
        r_root = self.find(right)
        if l_root == r_root:
            return
        if l_root < r_root:
            self.parent[r_root] = l_root
        else:
            self.parent[l_root] = r_root

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for value in self.parent:
            root = self.find(value)
            out.setdefault(root, []).append(value)
        return out


def _standalone_board_pads(project: Project, board_pads) -> list:
    if not board_pads:
        return []
    component_pad_points = _component_pad_positions(project)
    if not component_pad_points:
        return list(board_pads)

    index_grid_mm = 0.25
    match_tol_mm = 0.20
    indexed_points = _index_component_pad_points(component_pad_points, index_grid_mm)
    out = []
    for pad in board_pads:
        px = float(pad.at.x_mm)
        py = float(pad.at.y_mm)
        if _has_component_pad_match(
            indexed_points=indexed_points,
            x_mm=px,
            y_mm=py,
            grid_mm=index_grid_mm,
            tolerance_mm=match_tol_mm,
        ):
            continue
        out.append(pad)
    return out


def _component_pad_positions(project: Project) -> set[tuple[float, float]]:
    package_lookup = _package_lookup(project)
    board_pad_points: list[tuple[float, float]] = []
    if project.board is not None:
        board_pad_points = [
            (float(pad.at.x_mm), float(pad.at.y_mm))
            for pad in project.board.pads
        ]

    points: set[tuple[float, float]] = set()
    for component in project.components:
        package = _resolve_component_package(component, package_lookup)
        if package is None:
            continue
        effective_rotation_deg = _resolved_board_component_rotation_deg(
            component=component,
            source_format=project.source_format,
            package=package,
        )
        origin_x, origin_y = _component_move_point_mm(
            component,
            effective_rotation_deg=effective_rotation_deg,
        )

        transformed = _component_package_pad_world_points(
            component=component,
            package=package,
            origin_x_mm=float(origin_x),
            origin_y_mm=float(origin_y),
            rotation_deg=float(effective_rotation_deg or 0.0),
            board_pad_points=board_pad_points,
        )
        for ax, ay in transformed:
            points.add((round(ax, 3), round(ay, 3)))
    return points


def _component_package_pad_world_points(
    component,
    package: Package,
    origin_x_mm: float,
    origin_y_mm: float,
    rotation_deg: float,
    board_pad_points: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    variants = [(False, False), (True, False), (False, True), (True, True)]
    scored: list[tuple[float, list[tuple[float, float]]]] = []
    for mirror_x, mirror_y in variants:
        transformed = _transform_package_pad_points(
            component=component,
            package=package,
            origin_x_mm=origin_x_mm,
            origin_y_mm=origin_y_mm,
            rotation_deg=rotation_deg,
            mirror_x=mirror_x,
            mirror_y=mirror_y,
        )
        score = _pad_fit_score(transformed, board_pad_points)
        scored.append((score, transformed))

    scored.sort(key=lambda item: (item[0], len(item[1])))
    return scored[0][1] if scored else []


def _transform_package_pad_points(
    component,
    package: Package,
    origin_x_mm: float,
    origin_y_mm: float,
    rotation_deg: float,
    mirror_x: bool,
    mirror_y: bool,
) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    angle = math.radians(float(rotation_deg or 0.0))
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
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
        out.append((float(origin_x_mm) + rx, float(origin_y_mm) + ry))
    return out


def _pad_fit_score(
    transformed: list[tuple[float, float]],
    board_pad_points: list[tuple[float, float]],
) -> float:
    if not transformed:
        return float("inf")
    if not board_pad_points:
        return 0.0
    total = 0.0
    for x_mm, y_mm in transformed:
        best = min(
            (x_mm - px) * (x_mm - px) + (y_mm - py) * (y_mm - py)
            for px, py in board_pad_points
        )
        total += math.sqrt(best)
    return total / float(len(transformed))


def _index_component_pad_points(
    points: set[tuple[float, float]],
    grid_mm: float,
) -> dict[tuple[int, int], list[tuple[float, float]]]:
    index: dict[tuple[int, int], list[tuple[float, float]]] = {}
    if grid_mm <= 0.0:
        return index
    for x_mm, y_mm in points:
        key = (int(round(float(x_mm) / grid_mm)), int(round(float(y_mm) / grid_mm)))
        index.setdefault(key, []).append((float(x_mm), float(y_mm)))
    return index


def _has_component_pad_match(
    indexed_points: dict[tuple[int, int], list[tuple[float, float]]],
    x_mm: float,
    y_mm: float,
    grid_mm: float,
    tolerance_mm: float,
) -> bool:
    if not indexed_points or grid_mm <= 0.0:
        return False
    center_key = (int(round(float(x_mm) / grid_mm)), int(round(float(y_mm) / grid_mm)))
    tol_sq = float(tolerance_mm) * float(tolerance_mm)
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            bucket = indexed_points.get((center_key[0] + dx, center_key[1] + dy))
            if not bucket:
                continue
            for px, py in bucket:
                ddx = float(x_mm) - float(px)
                ddy = float(y_mm) - float(py)
                if (ddx * ddx + ddy * ddy) <= tol_sq:
                    return True
    return False


def _via_shape_from_pad_shape(shape: str) -> str:
    key = str(shape or "").strip().lower()
    if key in {"square", "rect"}:
        return "square"
    if key in {"octagon"}:
        return "octagon"
    return "round"


def _record_skipped_components_without_device(project: Project, skipped: list[str]) -> None:
    cleaned = sorted({item for item in skipped if item})
    if not cleaned:
        return

    already_reported = any(event.code == "BOARD_COMPONENT_SKIPPED_NO_DEVICE" for event in project.events)
    if already_reported:
        return

    project.events.append(
        project_event(
            Severity.WARNING,
            "BOARD_COMPONENT_SKIPPED_NO_DEVICE",
            "Board placement skipped components without resolved device IDs",
            {
                "count": len(cleaned),
                "components": cleaned[:200],
            },
        )
    )
