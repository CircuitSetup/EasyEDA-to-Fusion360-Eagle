from __future__ import annotations

from dataclasses import dataclass
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
_PAD_TOUCH_TOLERANCE_MM = 0.10
_PAD_NEAR_TOLERANCE_MM = 0.35
_PAD_CENTER_TOLERANCE_MM = 0.02


@dataclass(frozen=True)
class _PadAnchorTarget:
    refdes: str
    source_instance_id: str
    pad_number: str
    net_name: str
    layer: str
    center_x_mm: float
    center_y_mm: float
    width_mm: float
    height_mm: float
    rotation_deg: float
    shape: str
    drill_mm: float | None
    authoritative: bool


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
        pad_anchor_targets = _build_trace_pad_anchor_targets(project, package_lookup, net_alias)
        anchor_records: list[dict[str, object]] = []
        unresolved_touch_records: list[dict[str, object]] = []

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
        emitted_anchor_keys: set[tuple[str, str, str, str, str, str]] = set()
        seen_unresolved_touch_keys: set[tuple[str, str, str, str, str, str, str, str]] = set()
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
            if canonical_track_net and _is_copper_layer_num(layer_number):
                track_segment_key = _track_segment_key(
                    layer_number=layer_number,
                    net_name=canonical_track_net,
                    wire_width=wire_width,
                    start=(float(track.start.x_mm), float(track.start.y_mm)),
                    end=(float(track.end.x_mm), float(track.end.y_mm)),
                )
                if track_segment_key in emitted_anchor_keys:
                    continue
                emitted_anchor_keys.add(track_segment_key)
            lines.append(
                f"{wire_prefix} {wire_width:.4f} ({track.start.x_mm:.4f} {track.start.y_mm:.4f}) ({track.end.x_mm:.4f} {track.end.y_mm:.4f});"
            )
            if canonical_track_net and _is_copper_layer_num(layer_number):
                anchors, unresolved = _trace_pad_anchor_segments(
                    track=track,
                    track_layer_number=layer_number,
                    canonical_track_net=canonical_track_net,
                    pad_anchor_targets=pad_anchor_targets,
                    track_width_mm=float(track.width_mm or 0.0),
                )
                for issue in unresolved:
                    key = (
                        str(issue.get("net_name") or ""),
                        str(issue.get("track_layer") or ""),
                        f"{float(issue.get('track_endpoint_x_mm', 0.0)):.4f}",
                        f"{float(issue.get('track_endpoint_y_mm', 0.0)):.4f}",
                        str(issue.get("target_refdes") or ""),
                        str(issue.get("target_pad") or ""),
                        f"{float(issue.get('target_center_x_mm', 0.0)):.4f}",
                        f"{float(issue.get('target_center_y_mm', 0.0)):.4f}",
                    )
                    if key in seen_unresolved_touch_keys:
                        continue
                    seen_unresolved_touch_keys.add(key)
                    unresolved_touch_records.append(issue)
                for start, end, target in anchors:
                    anchor_key = _track_segment_key(
                        layer_number=layer_number,
                        net_name=canonical_track_net,
                        wire_width=wire_width,
                        start=start,
                        end=end,
                    )
                    if anchor_key in emitted_anchor_keys:
                        continue
                    emitted_anchor_keys.add(anchor_key)
                    lines.append(
                        f"WIRE {_quote_token(canonical_track_net)} {wire_width:.4f} ({start[0]:.4f} {start[1]:.4f}) ({end[0]:.4f} {end[1]:.4f});"
                    )
                    anchor_records.append(
                        {
                            "net_name": canonical_track_net,
                            "track_layer": layer_number,
                            "track_width_mm": wire_width,
                            "anchor_start_x_mm": start[0],
                            "anchor_start_y_mm": start[1],
                            "anchor_end_x_mm": end[0],
                            "anchor_end_y_mm": end[1],
                            "target_refdes": target.refdes,
                            "target_pad": target.pad_number,
                            "authoritative": target.authoritative,
                        }
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

        project.metadata["board_trace_pad_anchors"] = anchor_records
        project.metadata["board_trace_pad_anchor_count"] = len(anchor_records)
        project.metadata["board_trace_pad_touch_unresolved"] = unresolved_touch_records
        project.metadata["board_trace_pad_touch_unresolved_count"] = len(unresolved_touch_records)
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


def _track_segment_key(
    layer_number: str,
    net_name: str,
    wire_width: float,
    start: tuple[float, float],
    end: tuple[float, float],
) -> tuple[str, str, str, str, str, str]:
    sx = float(start[0])
    sy = float(start[1])
    ex = float(end[0])
    ey = float(end[1])
    if (ex < sx) or (abs(ex - sx) < 1e-9 and ey < sy):
        sx, sy, ex, ey = ex, ey, sx, sy

    return (
        str(layer_number),
        str(net_name or ""),
        f"{float(wire_width):.4f}",
        f"{sx:.4f}",
        f"{sy:.4f}",
        f"{ex:.4f},{ey:.4f}",
    )


def _build_trace_pad_anchor_targets(
    project: Project,
    package_lookup: dict[str, Package],
    net_alias: dict[str, str],
) -> list[_PadAnchorTarget]:
    board = project.board
    if board is None:
        return []

    board_pads = list(board.pads or [])
    board_pad_points = [
        (float(pad.at.x_mm), float(pad.at.y_mm))
        for pad in board_pads
    ]
    board_pad_lookup = _build_component_board_pad_lookup(board_pads)
    component_pin_nets = _component_pin_net_lookup(project, net_alias)

    targets: list[_PadAnchorTarget] = []
    authoritative_keys: set[tuple[str, str, str]] = set()
    for pad in board_pads:
        target = _authoritative_pad_anchor_target(pad, net_alias)
        if target is None:
            continue
        targets.append(target)
        authoritative_keys.add(
            (
                str(target.refdes or "").strip(),
                str(target.source_instance_id or "").strip(),
                str(target.pad_number or "").strip(),
            )
        )

    for component in project.components:
        package = _resolve_component_package(component, package_lookup)
        if package is None or not package.pads:
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
        mirror_x, mirror_y = _select_component_package_pad_variant(
            component=component,
            package=package,
            origin_x_mm=float(origin_x),
            origin_y_mm=float(origin_y),
            rotation_deg=float(effective_rotation_deg or 0.0),
            board_pad_points=board_pad_points,
        )

        refdes = str(component.refdes or "").strip()
        source_id = str(getattr(component, "source_instance_id", "") or "").strip()
        for pad in package.pads:
            pad_number = str(getattr(pad, "pad_number", "") or "").strip()
            if not pad_number:
                continue
            if _same_component_board_pad_exists(component, pad_number, board_pad_lookup):
                continue
            if (refdes, source_id, pad_number) in authoritative_keys:
                continue
            net_name = component_pin_nets.get((refdes, pad_number), "")
            if not net_name:
                continue
            targets.append(
                _transformed_pad_anchor_target(
                    component=component,
                    pad=pad,
                    net_name=net_name,
                    origin_x_mm=float(origin_x),
                    origin_y_mm=float(origin_y),
                    rotation_deg=float(effective_rotation_deg or 0.0),
                    mirror_x=mirror_x,
                    mirror_y=mirror_y,
                )
            )

    return targets


def _component_pin_net_lookup(project: Project, net_alias: dict[str, str]) -> dict[tuple[str, str], str]:
    pin_nets: dict[tuple[str, str], set[str]] = {}
    for net in project.nets:
        canonical = _canonical_net_name(net.name, net_alias)
        if not canonical:
            continue
        for node in net.nodes:
            refdes = str(node.refdes or "").strip()
            pin = str(node.pin or "").strip()
            if not refdes or not pin:
                continue
            pin_nets.setdefault((refdes, pin), set()).add(canonical)
    return {
        key: _pick_canonical_net_name(sorted(values))
        for key, values in pin_nets.items()
        if values
    }


def _build_component_board_pad_lookup(board_pads) -> dict[str, dict[tuple[str, ...], list]]:
    by_ref_source_pad: dict[tuple[str, str, str], list] = {}
    by_ref_pad: dict[tuple[str, str], list] = {}
    by_source_pad: dict[tuple[str, str], list] = {}

    for pad in board_pads:
        refdes = str(getattr(pad, "component_refdes", "") or "").strip()
        source_id = str(getattr(pad, "source_instance_id", "") or "").strip()
        pad_number = str(getattr(pad, "pad_number", "") or "").strip()
        if not pad_number:
            continue
        if refdes:
            by_ref_pad.setdefault((refdes, pad_number), []).append(pad)
            if source_id:
                by_ref_source_pad.setdefault((refdes, source_id, pad_number), []).append(pad)
        if source_id:
            by_source_pad.setdefault((source_id, pad_number), []).append(pad)

    return {
        "by_ref_source_pad": by_ref_source_pad,
        "by_ref_pad": by_ref_pad,
        "by_source_pad": by_source_pad,
    }


def _same_component_board_pad_exists(
    component,
    pad_number: str,
    board_pad_lookup: dict[str, dict[tuple[str, ...], list]],
) -> bool:
    refdes = str(getattr(component, "refdes", "") or "").strip()
    source_id = str(getattr(component, "source_instance_id", "") or "").strip()
    token = str(pad_number or "").strip()
    if not token:
        return False
    if refdes and source_id and board_pad_lookup["by_ref_source_pad"].get((refdes, source_id, token)):
        return True
    if refdes and board_pad_lookup["by_ref_pad"].get((refdes, token)):
        return True
    if source_id and board_pad_lookup["by_source_pad"].get((source_id, token)):
        return True
    return False


def _authoritative_pad_anchor_target(pad, net_alias: dict[str, str]) -> _PadAnchorTarget | None:
    refdes = str(getattr(pad, "component_refdes", "") or "").strip()
    source_id = str(getattr(pad, "source_instance_id", "") or "").strip()
    pad_number = str(getattr(pad, "pad_number", "") or "").strip()
    net_name = _canonical_net_name(getattr(pad, "net", None), net_alias)
    if not pad_number or not net_name:
        return None
    if not refdes and not source_id:
        return None
    return _PadAnchorTarget(
        refdes=refdes,
        source_instance_id=source_id,
        pad_number=pad_number,
        net_name=net_name,
        layer=str(getattr(pad, "layer", "") or ""),
        center_x_mm=float(pad.at.x_mm),
        center_y_mm=float(pad.at.y_mm),
        width_mm=max(float(getattr(pad, "width_mm", 0.0) or 0.0), 0.0),
        height_mm=max(float(getattr(pad, "height_mm", 0.0) or 0.0), 0.0),
        rotation_deg=float(getattr(pad, "rotation_deg", 0.0) or 0.0),
        shape=str(getattr(pad, "shape", "") or "rect"),
        drill_mm=float(getattr(pad, "drill_mm", 0.0)) if getattr(pad, "drill_mm", None) is not None else None,
        authoritative=True,
    )


def _transformed_pad_anchor_target(
    component,
    pad,
    net_name: str,
    origin_x_mm: float,
    origin_y_mm: float,
    rotation_deg: float,
    mirror_x: bool,
    mirror_y: bool,
) -> _PadAnchorTarget:
    center_x, center_y = _transform_package_local_point_to_world(
        component=component,
        x_mm=float(pad.at.x_mm),
        y_mm=float(pad.at.y_mm),
        origin_x_mm=origin_x_mm,
        origin_y_mm=origin_y_mm,
        rotation_deg=rotation_deg,
        mirror_x=mirror_x,
        mirror_y=mirror_y,
    )
    local_angle = math.radians(float(getattr(pad, "rotation_deg", 0.0) or 0.0))
    axis_x = float(pad.at.x_mm) + math.cos(local_angle)
    axis_y = float(pad.at.y_mm) + math.sin(local_angle)
    axis_world_x, axis_world_y = _transform_package_local_point_to_world(
        component=component,
        x_mm=axis_x,
        y_mm=axis_y,
        origin_x_mm=origin_x_mm,
        origin_y_mm=origin_y_mm,
        rotation_deg=rotation_deg,
        mirror_x=mirror_x,
        mirror_y=mirror_y,
    )
    world_rotation = math.degrees(math.atan2(axis_world_y - center_y, axis_world_x - center_x)) % 360.0
    return _PadAnchorTarget(
        refdes=str(getattr(component, "refdes", "") or "").strip(),
        source_instance_id=str(getattr(component, "source_instance_id", "") or "").strip(),
        pad_number=str(getattr(pad, "pad_number", "") or "").strip(),
        net_name=str(net_name or "").strip(),
        layer=str(getattr(pad, "layer", "") or ""),
        center_x_mm=float(center_x),
        center_y_mm=float(center_y),
        width_mm=max(float(getattr(pad, "width_mm", 0.0) or 0.0), 0.0),
        height_mm=max(float(getattr(pad, "height_mm", 0.0) or 0.0), 0.0),
        rotation_deg=float(world_rotation),
        shape=str(getattr(pad, "shape", "") or "rect"),
        drill_mm=float(getattr(pad, "drill_mm", 0.0)) if getattr(pad, "drill_mm", None) is not None else None,
        authoritative=False,
    )


def _trace_pad_anchor_segments(
    track,
    track_layer_number: str,
    canonical_track_net: str,
    pad_anchor_targets: list[_PadAnchorTarget],
    track_width_mm: float,
) -> tuple[list[tuple[tuple[float, float], tuple[float, float], _PadAnchorTarget]], list[dict[str, object]]]:
    if not canonical_track_net or not _is_copper_layer_num(track_layer_number):
        return [], []

    candidates = [
        target
        for target in pad_anchor_targets
        if target.net_name == canonical_track_net
        and _pad_target_supports_track_layer(target, track_layer_number)
    ]
    if not candidates:
        return [], []

    anchors: list[tuple[tuple[float, float], tuple[float, float], _PadAnchorTarget]] = []
    unresolved: list[dict[str, object]] = []
    endpoints = (
        ("start", (float(track.start.x_mm), float(track.start.y_mm))),
        ("end", (float(track.end.x_mm), float(track.end.y_mm))),
    )
    for endpoint_name, (x_mm, y_mm) in endpoints:
        target = _best_pad_target_for_endpoint(
            x_mm=x_mm,
            y_mm=y_mm,
            candidates=candidates,
            tolerance_mm=_PAD_TOUCH_TOLERANCE_MM,
            track_width_mm=track_width_mm,
        )
        if target is not None:
            if not _point_matches_pad_center(x_mm, y_mm, target, _PAD_CENTER_TOLERANCE_MM):
                anchors.append(
                    (
                        (target.center_x_mm, target.center_y_mm),
                        (x_mm, y_mm),
                        target,
                    )
                )
            continue

        near_target = _best_pad_target_for_endpoint(
            x_mm=x_mm,
            y_mm=y_mm,
            candidates=candidates,
            tolerance_mm=_PAD_NEAR_TOLERANCE_MM,
            track_width_mm=track_width_mm,
        )
        if near_target is None:
            continue
        if _point_matches_pad_center(x_mm, y_mm, near_target, _PAD_CENTER_TOLERANCE_MM):
            continue
        unresolved.append(
            {
                "net_name": canonical_track_net,
                "track_layer": str(track_layer_number),
                "endpoint": endpoint_name,
                "track_endpoint_x_mm": x_mm,
                "track_endpoint_y_mm": y_mm,
                "target_refdes": near_target.refdes,
                "target_pad": near_target.pad_number,
                "target_center_x_mm": near_target.center_x_mm,
                "target_center_y_mm": near_target.center_y_mm,
                "target_authoritative": near_target.authoritative,
            }
        )

    return anchors, unresolved


def _best_pad_target_for_endpoint(
    x_mm: float,
    y_mm: float,
    candidates: list[_PadAnchorTarget],
    tolerance_mm: float,
    track_width_mm: float,
) -> _PadAnchorTarget | None:
    best_target: _PadAnchorTarget | None = None
    best_key: tuple[int, float, str, str] | None = None
    for target in candidates:
        if not _point_touches_pad_copper(
            x_mm,
            y_mm,
            target,
            tolerance_mm=tolerance_mm,
            track_width_mm=track_width_mm,
        ):
            continue
        distance = math.hypot(x_mm - target.center_x_mm, y_mm - target.center_y_mm)
        sort_key = (
            0 if target.authoritative else 1,
            distance,
            str(target.refdes or ""),
            str(target.pad_number or ""),
        )
        if best_key is None or sort_key < best_key:
            best_key = sort_key
            best_target = target
    return best_target


def _point_matches_pad_center(
    x_mm: float,
    y_mm: float,
    target: _PadAnchorTarget,
    tolerance_mm: float,
) -> bool:
    return math.hypot(x_mm - target.center_x_mm, y_mm - target.center_y_mm) <= float(tolerance_mm)


def _pad_target_supports_track_layer(target: _PadAnchorTarget, track_layer_number: str) -> bool:
    if not _is_copper_layer_num(track_layer_number):
        return False
    if target.drill_mm is not None and float(target.drill_mm) > 0.0:
        return True
    pad_layer_number = _pad_copper_layer_number(target.layer)
    if not pad_layer_number:
        return False
    return str(pad_layer_number) == str(track_layer_number)


def _pad_copper_layer_number(layer_name: str) -> str:
    token = str(layer_name or "").strip().lower()
    if token in {"top_copper", "top", "toplayer", "1"}:
        return "1"
    if token in {"bottom_copper", "bottom", "bottomlayer", "2"}:
        return "16"
    layer_num = _layer_number(layer_name)
    if _is_copper_layer_num(layer_num):
        return layer_num
    return ""


def _point_touches_pad_copper(
    x_mm: float,
    y_mm: float,
    target: _PadAnchorTarget,
    tolerance_mm: float,
    track_width_mm: float,
) -> bool:
    if target.width_mm <= 0.0 or target.height_mm <= 0.0:
        return False
    local_x, local_y = _point_in_pad_local_frame(x_mm, y_mm, target)
    effective_tolerance_mm = _effective_pad_touch_tolerance(
        tolerance_mm=tolerance_mm,
        track_width_mm=track_width_mm,
    )
    return _pad_shape_contains_local_point(
        x_mm=local_x,
        y_mm=local_y,
        width_mm=target.width_mm,
        height_mm=target.height_mm,
        shape=target.shape,
        tolerance_mm=effective_tolerance_mm,
    )


def _effective_pad_touch_tolerance(
    tolerance_mm: float,
    track_width_mm: float,
) -> float:
    # EasyEDA track coordinates describe the trace centerline; during replay a
    # trace can already overlap pad copper even when the endpoint center is just
    # outside the pad body. Expand the pad touch test by half the track width so
    # we can add a minimal anchor for genuine same-net copper contact.
    return max(float(tolerance_mm), 0.0) + max(float(track_width_mm), 0.0) * 0.5


def _point_in_pad_local_frame(
    x_mm: float,
    y_mm: float,
    target: _PadAnchorTarget,
) -> tuple[float, float]:
    dx = float(x_mm) - float(target.center_x_mm)
    dy = float(y_mm) - float(target.center_y_mm)
    angle = math.radians(float(target.rotation_deg or 0.0))
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    local_x = dx * cos_a + dy * sin_a
    local_y = -dx * sin_a + dy * cos_a
    return local_x, local_y


def _pad_shape_contains_local_point(
    x_mm: float,
    y_mm: float,
    width_mm: float,
    height_mm: float,
    shape: str,
    tolerance_mm: float,
) -> bool:
    half_w = max(float(width_mm) * 0.5 + float(tolerance_mm), 0.0)
    half_h = max(float(height_mm) * 0.5 + float(tolerance_mm), 0.0)
    if half_w <= 0.0 or half_h <= 0.0:
        return False

    shape_key = str(shape or "").strip().lower()
    if shape_key in {"rect", "rectangle", "square"}:
        return abs(x_mm) <= half_w and abs(y_mm) <= half_h
    if shape_key in {"oval", "ellipse", "oblong", "long", "roundrect"}:
        return _capsule_contains_local_point(x_mm, y_mm, half_w, half_h)
    return ((x_mm / half_w) ** 2 + (y_mm / half_h) ** 2) <= 1.0 + 1e-9


def _capsule_contains_local_point(
    x_mm: float,
    y_mm: float,
    half_w: float,
    half_h: float,
) -> bool:
    if half_w <= half_h:
        core_half = max(half_h - half_w, 0.0)
        if abs(y_mm) <= core_half and abs(x_mm) <= half_w:
            return True
        cap_y = core_half if y_mm >= 0.0 else -core_half
        return (x_mm * x_mm) + ((y_mm - cap_y) * (y_mm - cap_y)) <= (half_w * half_w) + 1e-9

    core_half = max(half_w - half_h, 0.0)
    if abs(x_mm) <= core_half and abs(y_mm) <= half_h:
        return True
    cap_x = core_half if x_mm >= 0.0 else -core_half
    return ((x_mm - cap_x) * (x_mm - cap_x)) + (y_mm * y_mm) <= (half_h * half_h) + 1e-9


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
    mirror_x, mirror_y = _select_component_package_pad_variant(
        component=component,
        package=package,
        origin_x_mm=origin_x_mm,
        origin_y_mm=origin_y_mm,
        rotation_deg=rotation_deg,
        board_pad_points=board_pad_points,
    )
    return _transform_package_pad_points(
        component=component,
        package=package,
        origin_x_mm=origin_x_mm,
        origin_y_mm=origin_y_mm,
        rotation_deg=rotation_deg,
        mirror_x=mirror_x,
        mirror_y=mirror_y,
    )


def _select_component_package_pad_variant(
    component,
    package: Package,
    origin_x_mm: float,
    origin_y_mm: float,
    rotation_deg: float,
    board_pad_points: list[tuple[float, float]],
) -> tuple[bool, bool]:
    variants = ((False, False), (True, False), (False, True), (True, True))
    best_variant = variants[0]
    best_score = float("inf")
    best_length = 0
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
        key = (score, len(transformed))
        best_key = (best_score, best_length)
        if key < best_key:
            best_variant = (mirror_x, mirror_y)
            best_score = score
            best_length = len(transformed)
    return best_variant


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
    for pad in package.pads:
        out.append(
            _transform_package_local_point_to_world(
                component=component,
                x_mm=float(pad.at.x_mm),
                y_mm=float(pad.at.y_mm),
                origin_x_mm=origin_x_mm,
                origin_y_mm=origin_y_mm,
                rotation_deg=rotation_deg,
                mirror_x=mirror_x,
                mirror_y=mirror_y,
            )
        )
    return out


def _transform_package_local_point_to_world(
    component,
    x_mm: float,
    y_mm: float,
    origin_x_mm: float,
    origin_y_mm: float,
    rotation_deg: float,
    mirror_x: bool,
    mirror_y: bool,
) -> tuple[float, float]:
    px = float(x_mm)
    py = float(y_mm)
    if mirror_x:
        px = -px
    if mirror_y:
        py = -py
    if component.side == Side.BOTTOM:
        px = -px
    angle = math.radians(float(rotation_deg or 0.0))
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    rx = px * cos_a - py * sin_a
    ry = px * sin_a + py * cos_a
    return float(origin_x_mm) + rx, float(origin_y_mm) + ry


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
