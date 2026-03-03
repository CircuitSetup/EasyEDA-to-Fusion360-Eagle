from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from easyeda2fusion.builders.schematic_inference import SchematicInferenceReport
from easyeda2fusion.matchers.library_matcher import MatchContext
from easyeda2fusion.model import Project
from easyeda2fusion.utils.io import dump_json


@dataclass
class ValidationReport:
    converted_successfully: bool
    converted_with_warnings: bool
    lossy_conversions: list[str] = field(default_factory=list)
    unresolved_parts: list[str] = field(default_factory=list)
    ambiguous_mappings: list[str] = field(default_factory=list)
    inferred_schematic_items: list[str] = field(default_factory=list)
    manual_review_items: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)



def validate_project(
    project: Project,
    match_ctx: MatchContext,
    inference_report: SchematicInferenceReport | None,
) -> ValidationReport:
    lossy_layers = [layer.source_name for layer in project.layers if layer.lossy]

    unresolved = [part.refdes for part in match_ctx.unresolved_parts]
    ambiguous = [
        match.refdes
        for match in project.library_matches
        if not match.matched and match.candidates
    ]

    manual_review: list[str] = []
    package_lookup = {package.package_id: package for package in project.packages}
    package_lookup.update({package.name: package for package in project.packages})
    symbol_lookup = {symbol.symbol_id: symbol for symbol in project.symbols}
    device_lookup = {device.device_id: device for device in project.devices}

    component_pin_count: dict[str, int] = {}
    for net in project.nets:
        for node in net.nodes:
            ref = str(node.refdes or "").strip()
            if not ref:
                continue
            component_pin_count[ref] = component_pin_count.get(ref, 0) + 1

    if project.board is None:
        manual_review.append("Board missing from conversion output")
    else:
        if not project.board.outline:
            manual_review.append("Board outline missing or unsupported")
        for idx, region in enumerate(project.board.outline, start=1):
            if len(region.points) < 3:
                manual_review.append(f"Board outline region {idx} has fewer than 3 points")

    if not project.sheets:
        manual_review.append("No schematic sheets present")

    for component in project.components:
        if not component.device_id:
            manual_review.append(f"{component.refdes}: missing device mapping")
        if not component.package_id:
            manual_review.append(f"{component.refdes}: missing package mapping")
        if component.device_id and component.device_id.startswith("easyeda_generated:"):
            raw_device = component.device_id.split(":", 1)[1]
            if raw_device not in device_lookup:
                manual_review.append(f"{component.refdes}: generated device {raw_device} missing from project devices")
        if component.package_id and component.package_id not in package_lookup:
            manual_review.append(f"{component.refdes}: package {component.package_id} not found in package set")
        if component_pin_count.get(component.refdes, 0) == 0:
            manual_review.append(f"{component.refdes}: zero electrical connections in schematic netlist")

    for net in project.nets:
        if not net.nodes:
            manual_review.append(f"Net {net.name} has no explicit node connectivity")
            continue
        unique_nodes = {(node.refdes, node.pin) for node in net.nodes if node.refdes and node.pin}
        if len(unique_nodes) < 2 and not _is_power_ground_net(net.name):
            manual_review.append(f"Net {net.name} has fewer than 2 connected pins")

    for device in project.devices:
        symbol = symbol_lookup.get(device.symbol_id)
        if symbol is None:
            manual_review.append(f"Device {device.device_id}: missing symbol {device.symbol_id}")
            continue
        package = package_lookup.get(device.package_id) if device.package_id else None
        symbol_pin_names = {str(pin.pin_name or "").strip() for pin in symbol.pins if str(pin.pin_name or "").strip()}
        symbol_pin_numbers = {str(pin.pin_number or "").strip() for pin in symbol.pins if str(pin.pin_number or "").strip()}
        pad_numbers = {
            str(pad.pad_number).strip()
            for pad in package.pads
        } if package is not None else set()
        for pin_name, pad_name in device.pin_pad_map.items():
            pin_token = str(pin_name or "").strip()
            pad_token = str(pad_name or "").strip()
            if not pin_token or not pad_token:
                manual_review.append(f"Device {device.device_id}: empty pin/pad mapping entry")
                continue
            if pin_token not in symbol_pin_names and pin_token not in symbol_pin_numbers:
                manual_review.append(f"Device {device.device_id}: pin {pin_token} missing from symbol {symbol.symbol_id}")
            if package is not None and pad_token not in pad_numbers:
                manual_review.append(f"Device {device.device_id}: pad {pad_token} missing from package {package.name}")

    for component in project.components:
        if not _is_resistor_array_component(component):
            continue
        package = package_lookup.get(str(component.package_id or ""))
        if package is None:
            manual_review.append(f"{component.refdes}: resistor array package missing")
            continue
        if len(package.pads) < 4:
            manual_review.append(f"{component.refdes}: resistor array package has too few pads")

    board_nets = _board_net_names(project)
    schematic_nets = {str(net.name or "").strip() for net in project.nets if str(net.name or "").strip()}
    missing_board_nets = sorted(board_nets - schematic_nets)
    for name in missing_board_nets[:200]:
        manual_review.append(f"Board net {name} not present in schematic nets")

    inserted_supply = project.metadata.get("supply_symbols_inserted", [])
    supply_mode = str(project.metadata.get("supply_symbols_mode") or "enabled").strip().lower()
    if isinstance(inserted_supply, list):
        inserted_supply_nets = {
            str(item.get("net") or "").strip()
            for item in inserted_supply
            if isinstance(item, dict) and str(item.get("net") or "").strip()
        }
    else:
        inserted_supply_nets = set()
    recognized_power_nets = {
        str(net.name or "").strip()
        for net in project.nets
        if _is_power_ground_net(net.name)
    }
    if supply_mode != "disabled":
        for net_name in sorted(recognized_power_nets):
            if net_name not in inserted_supply_nets:
                manual_review.append(f"Power/GND net {net_name} has no inserted supply symbol")

    source_instance_total = len(project.components)
    placed_instance_total = sum(1 for component in project.components if component.device_id)
    if placed_instance_total != source_instance_total:
        manual_review.append(
            f"Board instance count mismatch: source={source_instance_total} placed={placed_instance_total}"
        )

    duplicate_refdes = sorted(
        refdes
        for refdes, count in _component_refdes_counts(project).items()
        if count > 1
    )
    for refdes in duplicate_refdes:
        manual_review.append(f"Duplicate component instance refdes detected: {refdes}")

    component_refdes = {str(component.refdes or "").strip() for component in project.components}
    generated_device_ids = {str(component.device_id or "") for component in project.components}
    required_refs_raw = project.metadata.get("required_component_refs")
    if isinstance(required_refs_raw, (list, tuple, set)):
        for ref in required_refs_raw:
            token = str(ref or "").strip()
            if not token:
                continue
            if token in component_refdes:
                continue
            manual_review.append(f"Expected component {token} not present in source component list")

    required_generated_raw = project.metadata.get("required_generated_devices")
    if isinstance(required_generated_raw, (list, tuple, set)):
        for device_name in required_generated_raw:
            token = str(device_name or "").strip()
            if not token:
                continue
            if any(token in item for item in generated_device_ids):
                continue
            manual_review.append(
                f"Expected generated device {token} not present in mapped component devices"
            )

    inferred_items: list[str] = []
    if inference_report is not None and inference_report.inferred:
        inferred_items.extend([f"net:{name}" for name in inference_report.inferred_nets])
        inferred_items.extend([f"uncertain_component:{ref}" for ref in inference_report.uncertain_components])
        manual_review.extend(inference_report.manual_review_items)

    organization_metrics = (
        project.metadata.get("schematic_organization_metrics", {})
        if isinstance(project.metadata, dict)
        else {}
    )
    if not isinstance(organization_metrics, dict):
        organization_metrics = {}
    overlap_count = int(organization_metrics.get("overlap_count", 0) or 0)
    orphan_label_count = int(organization_metrics.get("orphan_label_count", 0) or 0)
    disconnected_count = int(organization_metrics.get("disconnected_component_count", 0) or 0)
    if overlap_count > 0:
        manual_review.append(f"Schematic organization overlap_count={overlap_count}")
    if orphan_label_count > 0:
        manual_review.append(f"Schematic organization orphan_label_count={orphan_label_count}")
    if disconnected_count > 0:
        manual_review.append(f"Schematic organization disconnected_component_count={disconnected_count}")

    draw_metrics = (
        project.metadata.get("schematic_draw_metrics", {})
        if isinstance(project.metadata, dict)
        else {}
    )
    if not isinstance(draw_metrics, dict):
        draw_metrics = {}
    draw_unresolved_anchor_count = int(draw_metrics.get("unresolved_pin_anchor_count", 0) or 0)
    draw_orphan_endpoint_count = int(draw_metrics.get("orphan_wire_endpoint_count", 0) or 0)
    draw_label_only_count = int(draw_metrics.get("label_only_connection_count", 0) or 0)
    draw_disconnected_count = int(draw_metrics.get("disconnected_component_count", 0) or 0)
    if draw_unresolved_anchor_count > 0:
        manual_review.append(f"Schematic draw unresolved_pin_anchor_count={draw_unresolved_anchor_count}")
    if draw_orphan_endpoint_count > 0:
        manual_review.append(f"Schematic draw orphan_wire_endpoint_count={draw_orphan_endpoint_count}")
    if draw_label_only_count > 0:
        manual_review.append(f"Schematic draw label_only_connection_count={draw_label_only_count}")
    if draw_disconnected_count > 0:
        manual_review.append(f"Schematic draw disconnected_component_count={draw_disconnected_count}")

    has_warnings = bool(lossy_layers or unresolved or ambiguous or manual_review)

    report = ValidationReport(
        converted_successfully=True,
        converted_with_warnings=has_warnings,
        lossy_conversions=lossy_layers,
        unresolved_parts=unresolved,
        ambiguous_mappings=sorted(set(ambiguous)),
        inferred_schematic_items=inferred_items,
        manual_review_items=sorted(set(manual_review)),
        metrics={
            "component_count": len(project.components),
            "net_count": len(project.nets),
            "sheet_count": len(project.sheets),
            "device_count": len(project.devices),
            "package_count": len(project.packages),
            "layer_count": len(project.layers),
            "board_track_count": len(project.board.tracks) if project.board else 0,
            "board_via_count": len(project.board.vias) if project.board else 0,
            "board_pad_count": len(project.board.pads) if project.board else 0,
            "source_component_instance_count": source_instance_total,
            "placed_component_instance_count": placed_instance_total,
            "recognized_power_ground_nets": len(recognized_power_nets),
            "inserted_supply_symbols": len(inserted_supply_nets),
            "schematic_organization_block_count": int(organization_metrics.get("block_count", 0) or 0),
            "schematic_organization_repeated_channel_groups": int(organization_metrics.get("repeated_channel_groups", 0) or 0),
            "schematic_organization_crossing_risk_score": int(organization_metrics.get("crossing_risk_score", 0) or 0),
            "schematic_organization_overlap_count": overlap_count,
            "schematic_organization_orphan_label_count": orphan_label_count,
            "schematic_organization_disconnected_component_count": disconnected_count,
            "schematic_organization_power_net_count": len(organization_metrics.get("recognized_power_nets", [])),
            "schematic_organization_ground_net_count": len(organization_metrics.get("recognized_ground_nets", [])),
            "schematic_draw_symbol_count": int(draw_metrics.get("symbol_count", 0) or 0),
            "schematic_draw_connected_pin_count": int(draw_metrics.get("connected_pin_count", 0) or 0),
            "schematic_draw_wire_segment_count": int(draw_metrics.get("wire_segment_count", 0) or 0),
            "schematic_draw_junction_count": int(draw_metrics.get("junction_count", 0) or 0),
            "schematic_draw_orphan_wire_endpoint_count": draw_orphan_endpoint_count,
            "schematic_draw_unresolved_pin_anchor_count": draw_unresolved_anchor_count,
            "schematic_draw_label_only_connection_count": draw_label_only_count,
            "schematic_draw_disconnected_component_count": draw_disconnected_count,
        },
    )
    return report


def _is_power_ground_net(name: str) -> bool:
    token = "".join(ch for ch in str(name or "").upper() if ch.isalnum())
    if not token:
        return False
    if token in {
        "GND",
        "AGND",
        "DGND",
        "PGND",
        "EARTH",
        "CHASSIS",
        "3V3",
        "33V",
        "5V",
        "5V0",
        "12V",
        "VCC",
        "VDD",
        "VSS",
        "VBAT",
        "VIN",
        "AVDD",
        "DVDD",
    }:
        return True
    return token.startswith("VCC") or token.startswith("VDD")


def _is_resistor_array_component(component) -> bool:
    ref = str(component.refdes or "").upper()
    source = str(component.source_name or "").upper()
    attrs = component.attributes if isinstance(component.attributes, dict) else {}
    blob = " ".join(
        str(item or "").upper()
        for item in (
            source,
            attrs.get("Name"),
            attrs.get("Device"),
            attrs.get("Footprint"),
            attrs.get("Package"),
            attrs.get("component_class"),
        )
    )
    return (
        "RES-ARRAY" in blob
        or "RESISTORARRAY" in blob
        or "RESISTOR NETWORK" in blob
        or ref.startswith(("RN", "RA"))
    )


def _board_net_names(project: Project) -> set[str]:
    if project.board is None:
        return set()
    names = {
        str(track.net or "").strip()
        for track in project.board.tracks
        if str(track.net or "").strip()
    }
    names.update(
        str(via.net or "").strip()
        for via in project.board.vias
        if str(via.net or "").strip()
    )
    names.update(
        str(region.net or "").strip()
        for region in project.board.regions
        if str(region.net or "").strip()
    )
    return names


def _component_refdes_counts(project: Project) -> dict[str, int]:
    counts: dict[str, int] = {}
    for component in project.components:
        ref = str(component.refdes or "").strip()
        if not ref:
            continue
        counts[ref] = counts.get(ref, 0) + 1
    return counts


def write_validation_report(report: ValidationReport, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "validation_report.json"
    text_path = out_dir / "validation_report.txt"

    dump_json(json_path, asdict(report))

    lines = [
        "Validation Report",
        "",
        f"converted_successfully: {report.converted_successfully}",
        f"converted_with_warnings: {report.converted_with_warnings}",
        f"lossy_conversions: {len(report.lossy_conversions)}",
        f"unresolved_parts: {len(report.unresolved_parts)}",
        f"ambiguous_mappings: {len(report.ambiguous_mappings)}",
        f"inferred_schematic_items: {len(report.inferred_schematic_items)}",
        f"manual_review_items: {len(report.manual_review_items)}",
        "",
        "Metrics:",
    ]
    for key, value in sorted(report.metrics.items()):
        lines.append(f"- {key}: {value}")

    if report.manual_review_items:
        lines.append("")
        lines.append("Manual Review Items:")
        for item in report.manual_review_items:
            lines.append(f"- {item}")

    text_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json": json_path, "text": text_path}
