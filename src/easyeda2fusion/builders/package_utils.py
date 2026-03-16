from __future__ import annotations

import re
from typing import Any

from easyeda2fusion.builders.component_identity import sanitize_refdes
from easyeda2fusion.model import Package, Project


def package_lookup(project: Project) -> dict[str, Package]:
    lookup: dict[str, Package] = {}
    for package in project.packages:
        lookup[str(package.package_id)] = package
        if package.name:
            lookup[str(package.name)] = package
    return lookup


def resolve_component_package(component: Any, package_lookup: dict[str, Package]) -> Package | None:
    attrs = getattr(component, "attributes", {}) or {}
    for key in (
        component.package_id,
        attrs.get("package_name"),
        attrs.get("package"),
        attrs.get("Package"),
        attrs.get("footprint"),
        attrs.get("Footprint"),
    ):
        token = str(key or "").strip()
        if not token:
            continue
        pkg = package_lookup.get(token)
        if pkg is not None:
            return pkg
    return None


def package_pin_count(package: Package) -> int:
    pins = {
        str(pad.pad_number).strip()
        for pad in package.pads
        if str(pad.pad_number).strip()
    }
    return len(pins)


def component_is_resistor(component: Any) -> bool:
    ref = sanitize_refdes(str(getattr(component, "refdes", "") or "")).upper()
    if re.match(r"^R[0-9]", ref):
        return True
    source = str(getattr(component, "source_name", "") or "").upper()
    return "RES" in source


def canonicalize_two_pin_quarter_turn(rotation_deg: float) -> float:
    angle = int(round(float(rotation_deg or 0.0))) % 360
    if angle == 270:
        return 90.0
    return float(angle)


def is_adjustable_resistor_package(package: Package) -> bool:
    token = _norm_pkg_token(str(package.name or package.package_id or ""))
    return "RESADJ" in token or "TRIMMER" in token


def valid_pins_by_ref(project: Project) -> dict[str, set[str]]:
    package_pins: dict[str, set[str]] = {}
    for package in project.packages:
        pins = {
            str(pad.pad_number).strip()
            for pad in package.pads
            if str(pad.pad_number).strip()
        }
        package_pins[package.package_id] = pins
        package_pins[package.name] = pins

    valid: dict[str, set[str]] = {}
    for component in project.components:
        ref = sanitize_refdes(component.refdes)
        package_id = str(component.package_id or "").strip()
        if not package_id:
            valid[ref] = set()
            continue
        valid[ref] = set(package_pins.get(package_id, set()))
    return valid


def _norm_pkg_token(value: str) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())
