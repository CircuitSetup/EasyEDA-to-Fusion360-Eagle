from __future__ import annotations

import math
import re

from easyeda2fusion.model import Project, Severity, Side, project_event


class BoardReconstructionBuilder:
    """Builds EAGLE script command lines for board reconstruction."""

    def build_commands(self, project: Project) -> list[str]:
        lines: list[str] = [
            "GRID MM 0.05 ON;",
            "SET WIRE_BEND 2;",
        ]

        board = project.board
        if board is None:
            return lines
        net_alias = _build_track_net_aliases(board.tracks, board.vias)

        refdes_map = _build_refdes_map(project)
        valid_pins_by_ref = _valid_pins_by_ref(project)
        pad_count_by_package = _package_pad_count_lookup(project)
        placed_refs: set[str] = set()
        skipped_no_device: list[str] = []
        for component in project.components:
            if not str(component.device_id or "").strip():
                skipped_no_device.append(str(component.refdes or "").strip())
                continue
            safe_refdes = _resolve_component_refdes(component, refdes_map)
            placed_refs.add(safe_refdes)
            package_id = str(component.package_id or "").strip()
            pad_count = pad_count_by_package.get(package_id)
            lines.append(
                f"ROTATE ={_orientation_token(component.rotation_deg, component.side, pad_count=pad_count)} {safe_refdes};"
            )
            lines.append(f"MOVE {safe_refdes} ({component.at.x_mm:.4f} {component.at.y_mm:.4f});")
        _record_skipped_components_without_device(project, skipped_no_device)

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

        for outline in board.outline:
            lines.extend(_emit_region_wires(outline.layer, outline.points, width_mm=0.0, close=True))
        for cutout in board.cutouts:
            lines.extend(_emit_region_wires(cutout.layer or "46", cutout.points, width_mm=0.0, close=True))
        for keepout in board.keepouts:
            lines.extend(_emit_region_wires(keepout.layer or "41", keepout.points, width_mm=0.0, close=True))
        for region in board.regions:
            if _is_copper_polygon_region(region.layer, region.net, region.points):
                region_net = _canonical_net_name(region.net, net_alias)
                lines.extend(_emit_copper_polygon(region.layer, region_net, region.points))
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

            if layer_number != current_layer:
                lines.append(f"LAYER {layer_number};")
                current_layer = layer_number
            wire_prefix = "WIRE"
            canonical_track_net = _canonical_net_name(track.net, net_alias)
            if canonical_track_net and _is_copper_layer_num(layer_number):
                # Bind copper traces to explicit signal names to avoid interactive
                # "merge N$xx" prompts during script replay.
                wire_prefix = f"WIRE {_quote_token(canonical_track_net)}"
            lines.append(
                f"{wire_prefix} {max(track.width_mm, 0.01):.4f} ({track.start.x_mm:.4f} {track.start.y_mm:.4f}) ({track.end.x_mm:.4f} {track.end.y_mm:.4f});"
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
                rotation = int(round(float(text.rotation_deg or 0.0))) % 360
                orient = f"MR{rotation}" if mirrored else f"R{rotation}"
                lines.append(
                    f"TEXT '{payload}' ({text.at.x_mm:.4f} {text.at.y_mm:.4f}) {orient};"
                )

        lines.append("RATSNEST;")
        return lines


def _build_refdes_map(project: Project) -> dict[str, str]:
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for ordinal, component in enumerate(project.components, start=1):
        original = component.refdes
        base = _sanitize_refdes(original)
        candidate = base
        suffix_idx = 2
        while candidate in used:
            candidate = f"{base}_{suffix_idx}"
            suffix_idx += 1
        mapping.setdefault(original, candidate)
        mapping[_component_refdes_key(component, ordinal)] = candidate
        used.add(candidate)
    return mapping


def _component_refdes_key(component, ordinal: int) -> str:
    source_id = str(getattr(component, "source_instance_id", "") or "").strip()
    if source_id:
        return f"{component.refdes}::{source_id}"
    return f"{component.refdes}::IDX{ordinal}"


def _resolve_component_refdes(component, refdes_map: dict[str, str]) -> str:
    source_id = str(getattr(component, "source_instance_id", "") or "").strip()
    if source_id:
        keyed = refdes_map.get(f"{component.refdes}::{source_id}")
        if keyed:
            return keyed
    return refdes_map.get(component.refdes, _sanitize_refdes(component.refdes))


def _sanitize_refdes(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^A-Za-z0-9_]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    if not text:
        return "U_AUTO"
    if not text[0].isalpha():
        text = f"U_{text}"
    return text


def _quote_token(value: str) -> str:
    text = str(value or "").replace("'", "")
    return f"'{text}'"


def _layer_number(layer_name: str) -> str:
    key = str(layer_name or "").strip().lower()
    if key in {"top_copper", "1", "top", "toplayer"}:
        return "1"
    if key in {"bottom_copper", "2", "bottom", "bottomlayer"}:
        return "16"
    if key in {"3", "top_silkscreen", "topsilkscreen", "topsilklayer", "topsilkscreenlayer"}:
        return "21"
    if key in {"4", "bottom_silkscreen", "bottomsilkscreen", "bottomsilklayer", "bottomsilkscreenlayer"}:
        return "22"
    if key in {"5", "top_mask", "topsoldermasklayer"}:
        return "29"
    if key in {"6", "bottom_mask", "bottomsoldermasklayer"}:
        return "30"
    if key in {"7", "top_paste", "toppastemasklayer", "topsolderpastelayer"}:
        return "31"
    if key in {"8", "bottom_paste", "bottompastemasklayer", "bottomsolderpastelayer"}:
        return "32"
    if key in {"11", "dimension", "outline", "board_outline", "boardoutlinelayer"}:
        return "20"
    if key in {"39", "41", "keepout", "tkeepout", "trestrict"}:
        return "41"
    if key in {"40", "42", "bkeepout", "brestrict"}:
        return "42"
    if key in {"47", "56", "drill", "hole", "holedrawing", "drilldrawinglayer"}:
        return "44"
    if key in {"13", "14", "documentation", "mechanical", "t_docu"}:
        return "51"
    if key.startswith("inner"):
        digits = "".join(ch for ch in key if ch.isdigit())
        if digits:
            inner_idx = max(1, int(digits))
            if inner_idx <= 14:
                return str(1 + inner_idx)
            return "51"
    if key.isdigit():
        idx = int(key)
        if 15 <= idx <= 28:
            return str(idx - 13)
    return "51"


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
        lines.append(
            f"WIRE {max(width_mm, 0.0):.4f} ({start.x_mm:.4f} {start.y_mm:.4f}) ({end.x_mm:.4f} {end.y_mm:.4f});"
        )
    return lines


def _emit_copper_polygon(layer: str, net_name: str, points) -> list[str]:
    cleaned = _clean_points(points, close=True)
    if len(cleaned) < 3:
        return []
    layer_num = _layer_number(str(layer))
    if not _is_copper_layer_num(layer_num):
        return []
    coords = " ".join(f"({pt.x_mm:.4f} {pt.y_mm:.4f})" for pt in cleaned)
    return [
        f"LAYER {layer_num};",
        f"POLYGON {_quote_token(net_name)} 0 {coords};",
    ]


def _is_copper_polygon_region(layer: str, net_name: str | None, points) -> bool:
    if len(points) < 3:
        return False
    if not str(net_name or "").strip():
        return False
    layer_num = _layer_number(str(layer))
    return _is_copper_layer_num(layer_num)


def _is_copper_layer_num(layer_num: str) -> bool:
    try:
        idx = int(layer_num)
    except Exception:
        return False
    return idx == 1 or idx == 16 or 2 <= idx <= 15


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
        f"{max(track.width_mm, 0.01):.4f}",
        f"{sx:.4f}",
        f"{sy:.4f}",
        f"{ex:.4f},{ey:.4f}",
    )


def _valid_pins_by_ref(project: Project) -> dict[str, set[str]]:
    package_lookup: dict[str, set[str]] = {}
    for package in project.packages:
        pins = {
            str(pad.pad_number).strip()
            for pad in package.pads
            if str(pad.pad_number).strip()
        }
        package_lookup[package.package_id] = pins
        package_lookup[package.name] = pins

    valid: dict[str, set[str]] = {}
    for component in project.components:
        ref = _sanitize_refdes(component.refdes)
        package_id = str(component.package_id or "").strip()
        if not package_id:
            valid[ref] = set()
            continue
        valid[ref] = set(package_lookup.get(package_id, set()))
    return valid


def _orientation_token(
    rotation_deg: float,
    side: Side,
    *,
    pad_count: int | None = None,
) -> str:
    angle = int(round(float(rotation_deg or 0.0))) % 360
    # Canonicalize -90/270 to +90 for symmetric two-pin footprints.
    # Apply this uniformly so resistors/capacitors/other 2-pin parts
    # share identical rotation handling.
    if pad_count == 2 and angle == 270:
        angle = 90
    if side == Side.BOTTOM:
        return f"MR{angle}"
    return f"R{angle}"


def _package_pad_count_lookup(project: Project) -> dict[str, int]:
    counts: dict[str, int] = {}
    for package in project.packages:
        count = len(
            [
                pad
                for pad in package.pads
                if str(getattr(pad, "pad_number", "") or "").strip()
            ]
        )
        counts[str(package.package_id)] = count
        counts[str(package.name)] = count
    return counts


def _canonical_net_name(name: str | None, net_alias: dict[str, str]) -> str:
    raw = str(name or "").strip()
    if not raw:
        return ""
    return net_alias.get(raw, raw)


def _build_track_net_aliases(tracks, vias=None) -> dict[str, str]:
    candidate_tracks = [
        track
        for track in tracks
        if str(track.net or "").strip()
        and _is_copper_layer_num(_layer_number(track.layer))
    ]
    if len(candidate_tracks) < 2:
        return {}

    uf = _UnionFind()
    for track in candidate_tracks:
        uf.add(str(track.net).strip())

    for idx in range(len(candidate_tracks)):
        left = candidate_tracks[idx]
        left_net = str(left.net or "").strip()
        left_layer = _layer_number(left.layer)
        if not left_net:
            continue
        left_seg = ((float(left.start.x_mm), float(left.start.y_mm)), (float(left.end.x_mm), float(left.end.y_mm)))
        for jdx in range(idx + 1, len(candidate_tracks)):
            right = candidate_tracks[jdx]
            right_net = str(right.net or "").strip()
            if not right_net or right_net == left_net:
                continue
            if _layer_number(right.layer) != left_layer:
                continue
            right_seg = ((float(right.start.x_mm), float(right.start.y_mm)), (float(right.end.x_mm), float(right.end.y_mm)))
            if _segments_touch_or_overlap(left_seg, right_seg):
                uf.union(left_net, right_net)

    for via in vias or []:
        via_net = str(getattr(via, "net", "") or "").strip()
        touching: set[str] = set()
        if via_net:
            uf.add(via_net)
            touching.add(via_net)

        vx = float(via.at.x_mm)
        vy = float(via.at.y_mm)
        for track in candidate_tracks:
            track_net = str(track.net or "").strip()
            if not track_net:
                continue
            segment = (
                (float(track.start.x_mm), float(track.start.y_mm)),
                (float(track.end.x_mm), float(track.end.y_mm)),
            )
            if _point_on_segment((vx, vy), segment):
                touching.add(track_net)

        if len(touching) >= 2:
            touching_list = sorted(touching)
            base = touching_list[0]
            for other in touching_list[1:]:
                uf.union(base, other)

    groups = uf.groups()
    aliases: dict[str, str] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        canonical = _pick_canonical_net_name(members)
        for member in members:
            aliases[member] = canonical
    return aliases


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
    out = []
    for pad in board_pads:
        key = (round(float(pad.at.x_mm), 3), round(float(pad.at.y_mm), 3))
        if key in component_pad_points:
            continue
        out.append(pad)
    return out


def _component_pad_positions(project: Project) -> set[tuple[float, float]]:
    package_lookup = {}
    for package in project.packages:
        package_lookup[package.package_id] = package
        package_lookup[package.name] = package

    points: set[tuple[float, float]] = set()
    for component in project.components:
        package_id = str(component.package_id or "").strip()
        if not package_id:
            continue
        package = package_lookup.get(package_id)
        if package is None:
            continue
        angle = math.radians(float(component.rotation_deg or 0.0))
        cos_a = math.cos(angle)
        sin_a = math.sin(angle)
        for pad in package.pads:
            px = float(pad.at.x_mm)
            py = float(pad.at.y_mm)
            if component.side == Side.BOTTOM:
                px = -px
            rx = px * cos_a - py * sin_a
            ry = px * sin_a + py * cos_a
            ax = float(component.at.x_mm) + rx
            ay = float(component.at.y_mm) + ry
            points.add((round(ax, 3), round(ay, 3)))
    return points


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
