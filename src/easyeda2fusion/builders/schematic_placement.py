from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from easyeda2fusion.model import Net, Project


@dataclass(frozen=True)
class BoardDerivedPlacementEntry:
    refdes: str
    component_instance_id: str
    schematic_x_mm: float
    schematic_y_mm: float
    schematic_rotation_deg: float
    schematic_mirrored: bool
    board_x_mm: float
    board_y_mm: float
    board_side: str
    block_id: str
    block_kind: str
    neighbor_refs: tuple[str, ...]
    rationale: str


@dataclass
class BoardDerivedPlacementMap:
    entries: tuple[BoardDerivedPlacementEntry, ...]

    def as_report_dict(self) -> dict[str, Any]:
        return {
            "component_count": len(self.entries),
            "entries": [
                {
                    "refdes": item.refdes,
                    "component_instance_id": item.component_instance_id,
                    "schematic_origin_mm": {"x": item.schematic_x_mm, "y": item.schematic_y_mm},
                    "schematic_rotation_deg": item.schematic_rotation_deg,
                    "schematic_mirrored": item.schematic_mirrored,
                    "board_origin_mm": {"x": item.board_x_mm, "y": item.board_y_mm},
                    "board_side": item.board_side,
                    "block_id": item.block_id,
                    "block_kind": item.block_kind,
                    "neighbor_refs": list(item.neighbor_refs),
                    "rationale": item.rationale,
                }
                for item in self.entries
            ],
        }


def build_board_derived_placement_map(
    project: Project,
    refdes_map: dict[str, str],
    placement_map: dict[str, tuple[float, float]],
    effective_nets: list[Net],
    layout_metadata: dict[str, Any],
) -> BoardDerivedPlacementMap:
    block_id_by_ref: dict[str, str] = {}
    block_kind_by_ref: dict[str, str] = {}
    blocks = list(layout_metadata.get("blocks", [])) if isinstance(layout_metadata, dict) else []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_id = str(block.get("id") or "").strip() or "block_0"
        block_kind = str(block.get("kind") or "").strip() or "cluster"
        for ref in block.get("components", []) or []:
            token = str(ref or "").strip()
            if not token:
                continue
            block_id_by_ref[token] = block_id
            block_kind_by_ref[token] = block_kind

    neighbors = _neighbors_from_nets(effective_nets, refdes_map)

    entries: list[BoardDerivedPlacementEntry] = []
    for component in sorted(project.components, key=lambda item: _component_sort_key(refdes_map.get(str(item.refdes or ""), str(item.refdes or "")))):
        ref = refdes_map.get(str(component.refdes or ""), str(component.refdes or ""))
        if not str(component.device_id or "").strip():
            continue
        sx, sy = placement_map.get(ref, (component.at.x_mm, component.at.y_mm))
        block_id = block_id_by_ref.get(ref, "block_0")
        block_kind = block_kind_by_ref.get(ref, str(layout_metadata.get("mode") or "board"))
        neighbor_refs = tuple(sorted(neighbors.get(ref, set()), key=_component_sort_key)[:8])
        entries.append(
            BoardDerivedPlacementEntry(
                refdes=ref,
                component_instance_id=str(component.source_instance_id or ref),
                schematic_x_mm=float(sx),
                schematic_y_mm=float(sy),
                schematic_rotation_deg=float(component.rotation_deg or 0.0),
                schematic_mirrored=bool(str(component.side).lower().endswith("bottom")),
                board_x_mm=float(component.at.x_mm),
                board_y_mm=float(component.at.y_mm),
                board_side=str(component.side.value if hasattr(component.side, "value") else component.side),
                block_id=block_id,
                block_kind=block_kind,
                neighbor_refs=neighbor_refs,
                rationale=_placement_rationale(block_kind, neighbor_refs),
            )
        )

    return BoardDerivedPlacementMap(entries=tuple(entries))


def _neighbors_from_nets(effective_nets: list[Net], refdes_map: dict[str, str]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for net in effective_nets:
        refs = {
            refdes_map.get(str(node.refdes or ""), str(node.refdes or ""))
            for node in net.nodes
            if str(node.refdes or "").strip()
        }
        refs = {ref for ref in refs if ref}
        for ref in refs:
            out[ref].update(other for other in refs if other != ref)
    return out


def _placement_rationale(block_kind: str, neighbor_refs: tuple[str, ...]) -> str:
    if neighbor_refs:
        return f"board_locality+net_cluster:{block_kind}"
    return f"board_locality:{block_kind}"


def _component_sort_key(ref: str) -> tuple[str, int, str]:
    token = str(ref or "").strip()
    prefix = "".join(ch for ch in token if ch.isalpha()).upper()
    digits = "".join(ch for ch in token if ch.isdigit())
    if digits:
        return (prefix, int(digits), token)
    return (prefix, 0, token)

