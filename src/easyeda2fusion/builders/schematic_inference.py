from __future__ import annotations

from dataclasses import dataclass, field
import math

from easyeda2fusion.builders.net_aliases import build_track_net_aliases
from easyeda2fusion.model import Net, NetNode, Package, Point, Project, Side, SchematicSheet, Severity, project_event


@dataclass(frozen=True)
class _PadTransformVariant:
    name: str
    mirror_x: bool
    mirror_y: bool


_PAD_TRANSFORM_VARIANTS: tuple[_PadTransformVariant, ...] = (
    _PadTransformVariant("direct", False, False),
    _PadTransformVariant("mirror_x", True, False),
    _PadTransformVariant("mirror_y", False, True),
    _PadTransformVariant("mirror_xy", True, True),
)


@dataclass
class SchematicInferenceReport:
    inferred: bool = False
    inferred_nets: list[str] = field(default_factory=list)
    ambiguous_pin_mappings: list[str] = field(default_factory=list)
    uncertain_components: list[str] = field(default_factory=list)
    manual_review_items: list[str] = field(default_factory=list)


def infer_schematic_from_board(project: Project, force: bool = False) -> SchematicInferenceReport:
    report = SchematicInferenceReport(inferred=False)
    if project.board is None:
        report.manual_review_items.append("No board data available for inference")
        return report

    if project.sheets and not force:
        # Source schematic exists; keep logical intent authoritative.
        return report

    inferred_sheet = SchematicSheet(sheet_id="inferred_sheet_1", name="INFERRED_FROM_BOARD")
    inferred_sheet.components = [
        component.refdes
        for component in project.components
        if _is_meaningful_refdes(component.refdes)
    ]
    project.sheets.append(inferred_sheet)
    report.inferred = True

    board = project.board
    net_alias = build_track_net_aliases(board.tracks, board.vias)
    inferred_nodes = _infer_board_pin_nodes(project, net_alias)

    # Include named copper features even when no pin-node mapping could be inferred.
    named_board_nets = {
        _canonical_net_name(track.net, net_alias)
        for track in board.tracks
        if str(track.net or "").strip()
    }
    named_board_nets.update(
        _canonical_net_name(via.net, net_alias)
        for via in board.vias
        if str(via.net or "").strip()
    )
    named_board_nets.update(
        _canonical_net_name(region.net, net_alias)
        for region in board.regions
        if str(region.net or "").strip()
    )
    named_board_nets = {name for name in named_board_nets if name}

    existing_lookup: dict[str, Net] = {}
    for net in project.nets:
        canonical = _canonical_net_name(net.name, net_alias)
        if not canonical:
            continue
        if not net.name:
            net.name = canonical
        elif net.name != canonical:
            net.name = canonical
        existing_lookup.setdefault(canonical, net)

    node_owner: dict[tuple[str, str], str] = {}
    for net_name, net in existing_lookup.items():
        pruned_nodes: list[NetNode] = []
        for node in net.nodes:
            key = (str(node.refdes or "").strip(), str(node.pin or "").strip())
            if not key[0] or not key[1]:
                continue
            owner = node_owner.get(key)
            if owner is None:
                node_owner[key] = net_name
                pruned_nodes.append(node)
            elif owner == net_name:
                pruned_nodes.append(node)
            else:
                # Keep first-seen owner deterministically; board inference may override below.
                continue
        net.nodes = pruned_nodes

    skipped_conflicting_nodes = 0
    for net_name, nodes in sorted(inferred_nodes.items()):
        target = existing_lookup.get(net_name)
        if target is None:
            target = Net(name=net_name, nodes=[])
            project.nets.append(target)
            existing_lookup[net_name] = target
            report.inferred_nets.append(net_name)

        seen = {(node.refdes, node.pin) for node in target.nodes}
        for node in nodes:
            key = (str(node.refdes or "").strip(), str(node.pin or "").strip())
            if not key[0] or not key[1]:
                continue
            owner = node_owner.get(key)
            if owner and owner != net_name:
                skipped_conflicting_nodes += 1
                continue
            if key in seen:
                node_owner[key] = net_name
                continue
            target.nodes.append(NetNode(refdes=key[0], pin=key[1]))
            seen.add(key)
            node_owner[key] = net_name

    for net_name in sorted(named_board_nets):
        if net_name in existing_lookup:
            continue
        project.nets.append(Net(name=net_name, nodes=[]))
        existing_lookup[net_name] = project.nets[-1]
        report.inferred_nets.append(net_name)

    removed_invalid = _prune_invalid_pin_nodes(project)
    if removed_invalid:
        report.manual_review_items.append(
            f"Removed {removed_invalid} invalid source schematic pin-node references during board-driven inference"
        )
        project.events.append(
            project_event(
                Severity.WARNING,
                "INFERENCE_INVALID_PIN_NODES_PRUNED",
                "Pruned invalid schematic pin-node references that do not exist in resolved package pads",
                {"removed_nodes": removed_invalid},
            )
        )

    if skipped_conflicting_nodes:
        report.manual_review_items.append(
            f"Skipped {skipped_conflicting_nodes} conflicting inferred pin-to-net nodes that disagreed with existing source net assignments"
        )
        project.events.append(
            project_event(
                Severity.WARNING,
                "INFERENCE_PIN_NET_CONFLICT_SKIPPED",
                "Board-driven inference skipped inferred pin-to-net assignments that conflicted with existing source net nodes",
                {"skipped_nodes": skipped_conflicting_nodes},
            )
        )

    for component in project.components:
        if not _is_meaningful_refdes(component.refdes):
            continue
        if not component.symbol_id:
            report.uncertain_components.append(component.refdes)
        if not component.package_id:
            report.manual_review_items.append(
                f"{component.refdes}: package missing for inferred schematic context"
            )

    if report.inferred_nets:
        project.events.append(
            project_event(
                Severity.WARNING,
                "SCHEMATIC_INFERRED_FROM_BOARD",
                "Schematic reconstructed from PCB connectivity; review inferred nets and mappings",
                {
                    "inferred_nets": report.inferred_nets,
                    "uncertain_components": report.uncertain_components,
                },
            )
        )

    if not project.nets:
        report.manual_review_items.append("No named nets found; board traces may be unlabeled")

    # Explicitly flag that logical pin connectivity could not be guaranteed.
    report.ambiguous_pin_mappings.extend(
        f"{component.refdes}: pin-to-net mapping could not be fully inferred"
        for component in project.components
        if _is_meaningful_refdes(component.refdes)
    )
    return report


def _prune_invalid_pin_nodes(project: Project) -> int:
    package_lookup: dict[str, set[str]] = {}
    for package in project.packages:
        pins = {
            str(pad.pad_number or "").strip()
            for pad in package.pads
            if str(pad.pad_number or "").strip()
        }
        package_lookup[package.package_id] = pins
        package_lookup[package.name] = pins

    component_valid_pins: dict[str, set[str]] = {}
    for component in project.components:
        ref = str(component.refdes or "").strip()
        package_id = str(component.package_id or "").strip()
        if not ref or not package_id:
            continue
        component_valid_pins[ref] = set(package_lookup.get(package_id, set()))

    removed = 0
    for net in project.nets:
        filtered: list[NetNode] = []
        for node in net.nodes:
            ref = str(node.refdes or "").strip()
            pin = str(node.pin or "").strip()
            if not ref or not pin:
                removed += 1
                continue
            valid = component_valid_pins.get(ref, set())
            if valid and pin not in valid:
                removed += 1
                continue
            filtered.append(node)
        net.nodes = filtered
    return removed


def _is_meaningful_refdes(refdes: str) -> bool:
    text = str(refdes or "").strip()
    if not text:
        return False
    return not (text.startswith("e") and text[1:].isdigit())


def _canonical_net_name(name: str | None, net_alias: dict[str, str]) -> str:
    raw = str(name or "").strip()
    if not raw:
        return ""
    return net_alias.get(raw, raw)


def _infer_board_pin_nodes(project: Project, net_alias: dict[str, str]) -> dict[str, list[NetNode]]:
    if project.board is None:
        return {}

    board = project.board
    package_lookup: dict[str, Package] = {}
    for package in project.packages:
        package_lookup[package.package_id] = package
        package_lookup[package.name] = package

    board_pads_all = list(board.pads or [])
    board_pad_lookup = _build_board_pad_lookup(board_pads_all)
    board_pads = [pad for pad in board_pads_all if str(pad.net or "").strip()]
    board_vias = [via for via in board.vias if str(via.net or "").strip()]
    board_tracks = [
        track
        for track in board.tracks
        if str(track.net or "").strip()
    ]

    inferred: dict[str, list[NetNode]] = {}
    seen_nodes: set[tuple[str, str, str]] = set()

    for component in project.components:
        refdes = str(component.refdes or "").strip()
        if not refdes:
            continue

        package_id = str(component.package_id or "").strip()
        if not package_id:
            continue
        package = package_lookup.get(package_id)
        if package is None:
            continue

        transform = _select_component_pad_transform(
            component=component,
            package=package,
            board_pad_lookup=board_pad_lookup,
        )
        for pad in package.pads:
            pad_number = str(pad.pad_number or "").strip()
            if not pad_number:
                continue
            net_name = ""
            same_component_board_pads = _same_component_board_pad_candidates(
                component=component,
                pad_number=pad_number,
                board_pad_lookup=board_pad_lookup,
            )
            if same_component_board_pads:
                matched_board_pad = _best_matching_board_pad(
                    component=component,
                    pad_point=pad.at,
                    board_pad_candidates=same_component_board_pads,
                    transform=transform,
                )
                matched_board_net = _canonical_net_name(matched_board_pad.net, net_alias)
                if matched_board_net:
                    net_name = matched_board_net
                else:
                    direct_world = _component_pad_world_point(component, pad.at)
                    net_name = _closest_board_copper_touch(
                        world=direct_world,
                        board_vias=board_vias,
                        board_tracks=board_tracks,
                        net_alias=net_alias,
                    )
            else:
                world = _component_pad_world_point(
                    component,
                    pad.at,
                    mirror_x=transform.mirror_x,
                    mirror_y=transform.mirror_y,
                )
                net_name = _closest_board_net(world, board_pads, board_vias, board_tracks, net_alias)
            if not net_name:
                continue
            dedupe_key = (net_name, refdes, pad_number)
            if dedupe_key in seen_nodes:
                continue
            seen_nodes.add(dedupe_key)
            inferred.setdefault(net_name, []).append(NetNode(refdes=refdes, pin=pad_number))

    return inferred


def _component_pad_world_point(
    component,
    pad_point: Point,
    *,
    mirror_x: bool = False,
    mirror_y: bool = False,
) -> Point:
    px = float(pad_point.x_mm)
    py = float(pad_point.y_mm)
    if mirror_x:
        px = -px
    if mirror_y:
        py = -py
    if component.side == Side.BOTTOM:
        px = -px
    # Package-local pads in the normalized model are derived by rotating source
    # absolute pad coordinates by -component.rotation during parsing. Reprojecting
    # to board world coordinates must therefore apply +component.rotation.
    angle = math.radians(float(component.rotation_deg or 0.0))
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    rx = px * cos_a - py * sin_a
    ry = px * sin_a + py * cos_a
    return Point(
        x_mm=float(component.at.x_mm) + rx,
        y_mm=float(component.at.y_mm) + ry,
    )


def _component_pad_world_point_candidates(component, pad_point: Point) -> list[Point]:
    candidates = [
        _component_pad_world_point(component, pad_point),
        _component_pad_world_point(component, pad_point, mirror_x=True),
        _component_pad_world_point(component, pad_point, mirror_y=True),
        _component_pad_world_point(component, pad_point, mirror_x=True, mirror_y=True),
    ]
    unique: list[Point] = []
    seen: set[tuple[float, float]] = set()
    for point in candidates:
        key = (round(float(point.x_mm), 6), round(float(point.y_mm), 6))
        if key in seen:
            continue
        seen.add(key)
        unique.append(point)
    return unique


def _build_board_pad_lookup(board_pads: list) -> dict[str, dict[tuple[str, ...], list]]:
    by_ref_source_pad: dict[tuple[str, str, str], list] = {}
    by_ref_pad: dict[tuple[str, str], list] = {}
    by_source_pad: dict[tuple[str, str], list] = {}
    by_pad: dict[str, list] = {}

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
        by_pad.setdefault(pad_number, []).append(pad)

    return {
        "by_ref_source_pad": by_ref_source_pad,
        "by_ref_pad": by_ref_pad,
        "by_source_pad": by_source_pad,
        "by_pad": by_pad,
    }


def _same_component_board_pad_candidates(
    component,
    pad_number: str,
    board_pad_lookup: dict[str, dict[tuple[str, ...], list]],
) -> list:
    refdes = str(getattr(component, "refdes", "") or "").strip()
    source_id = str(getattr(component, "source_instance_id", "") or "").strip()

    out: list = []
    seen: set[int] = set()

    def _append(items: list | None) -> None:
        for item in items or []:
            marker = id(item)
            if marker in seen:
                continue
            seen.add(marker)
            out.append(item)

    if refdes and source_id:
        _append(board_pad_lookup["by_ref_source_pad"].get((refdes, source_id, pad_number)))
    if refdes:
        _append(board_pad_lookup["by_ref_pad"].get((refdes, pad_number)))
    if not out and source_id:
        _append(board_pad_lookup["by_source_pad"].get((source_id, pad_number)))
    return out


def _candidate_transform_board_pads(
    component,
    pad_number: str,
    board_pad_lookup: dict[str, dict[tuple[str, ...], list]],
) -> list:
    same_component = _same_component_board_pad_candidates(component, pad_number, board_pad_lookup)
    if same_component:
        return same_component
    return list(board_pad_lookup["by_pad"].get(pad_number, ()))


def _select_component_pad_transform(
    component,
    package: Package,
    board_pad_lookup: dict[str, dict[tuple[str, ...], list]],
) -> _PadTransformVariant:
    best_variant = _PAD_TRANSFORM_VARIANTS[0]
    best_score: tuple[int, float, int] | None = None

    for ordinal, variant in enumerate(_PAD_TRANSFORM_VARIANTS):
        matched_count = 0
        total_distance = 0.0
        for pad in package.pads:
            pad_number = str(getattr(pad, "pad_number", "") or "").strip()
            if not pad_number:
                continue
            candidates = _candidate_transform_board_pads(component, pad_number, board_pad_lookup)
            if not candidates:
                continue
            world = _component_pad_world_point(
                component,
                pad.at,
                mirror_x=variant.mirror_x,
                mirror_y=variant.mirror_y,
            )
            nearest = min(
                (
                    math.hypot(
                        float(world.x_mm) - float(candidate.at.x_mm),
                        float(world.y_mm) - float(candidate.at.y_mm),
                    )
                    for candidate in candidates
                ),
                default=None,
            )
            if nearest is None:
                continue
            if nearest <= 0.35:
                matched_count += 1
                total_distance += nearest
        score = (-matched_count, total_distance, ordinal)
        if best_score is None or score < best_score:
            best_score = score
            best_variant = variant
    return best_variant


def _best_matching_board_pad(
    component,
    pad_point: Point,
    board_pad_candidates: list,
    transform: _PadTransformVariant,
):
    expected = _component_pad_world_point(
        component,
        pad_point,
        mirror_x=transform.mirror_x,
        mirror_y=transform.mirror_y,
    )
    return min(
        board_pad_candidates,
        key=lambda candidate: (
            math.hypot(
                float(expected.x_mm) - float(candidate.at.x_mm),
                float(expected.y_mm) - float(candidate.at.y_mm),
            ),
            round(float(candidate.at.x_mm), 6),
            round(float(candidate.at.y_mm), 6),
            str(getattr(candidate, "net", "") or ""),
        ),
    )


def _closest_board_net(world: Point, board_pads, board_vias, board_tracks, net_alias: dict[str, str]) -> str:
    x = float(world.x_mm)
    y = float(world.y_mm)

    pad_tol = 0.30
    via_tol = 0.30
    track_tol = 0.20

    best_pad: tuple[float, str] | None = None
    for pad in board_pads:
        pad_net = _canonical_net_name(pad.net, net_alias)
        if not pad_net:
            continue
        dist = math.hypot(x - float(pad.at.x_mm), y - float(pad.at.y_mm))
        if dist <= pad_tol and (best_pad is None or dist < best_pad[0]):
            best_pad = (dist, pad_net)
    if best_pad is not None:
        return best_pad[1]

    best_via: tuple[float, str] | None = None
    for via in board_vias:
        via_net = _canonical_net_name(via.net, net_alias)
        if not via_net:
            continue
        dist = math.hypot(x - float(via.at.x_mm), y - float(via.at.y_mm))
        if dist <= via_tol and (best_via is None or dist < best_via[0]):
            best_via = (dist, via_net)
    if best_via is not None:
        return best_via[1]

    best_track: tuple[float, str] | None = None
    for track in board_tracks:
        track_net = _canonical_net_name(track.net, net_alias)
        if not track_net:
            continue
        distance = _distance_point_to_segment(
            x,
            y,
            float(track.start.x_mm),
            float(track.start.y_mm),
            float(track.end.x_mm),
            float(track.end.y_mm),
        )
        if distance <= track_tol and (best_track is None or distance < best_track[0]):
            best_track = (distance, track_net)
    if best_track is not None:
        return best_track[1]

    return ""


def _closest_board_copper_touch(world: Point, board_vias, board_tracks, net_alias: dict[str, str]) -> str:
    x = float(world.x_mm)
    y = float(world.y_mm)

    best_via: tuple[float, str] | None = None
    for via in board_vias:
        via_net = _canonical_net_name(via.net, net_alias)
        if not via_net:
            continue
        dist = math.hypot(x - float(via.at.x_mm), y - float(via.at.y_mm))
        if dist <= 0.10 and (best_via is None or dist < best_via[0]):
            best_via = (dist, via_net)
    if best_via is not None:
        return best_via[1]

    best_track: tuple[float, str] | None = None
    for track in board_tracks:
        track_net = _canonical_net_name(track.net, net_alias)
        if not track_net:
            continue
        distance = _distance_point_to_segment(
            x,
            y,
            float(track.start.x_mm),
            float(track.start.y_mm),
            float(track.end.x_mm),
            float(track.end.y_mm),
        )
        if distance <= 0.10 and (best_track is None or distance < best_track[0]):
            best_track = (distance, track_net)
    if best_track is not None:
        return best_track[1]

    return ""


def _distance_point_to_segment(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
    dx = x2 - x1
    dy = y2 - y1
    if abs(dx) < 1e-12 and abs(dy) < 1e-12:
        return math.hypot(px - x1, py - y1)

    t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    proj_x = x1 + t * dx
    proj_y = y1 + t * dy
    return math.hypot(px - proj_x, py - proj_y)
