from __future__ import annotations

import re
from typing import Any

from easyeda2fusion.model import Project


def sanitize_refdes(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^A-Za-z0-9_]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    if not text:
        return "U_AUTO"
    if not text[0].isalpha():
        text = f"U_{text}"
    return text


def component_instance_key(component: Any, ordinal: int) -> str:
    source_id = str(getattr(component, "source_instance_id", "") or "").strip()
    if source_id:
        return f"{component.refdes}::{source_id}"
    return f"{component.refdes}::IDX{ordinal}"


def build_refdes_map(project: Project) -> dict[str, str]:
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for ordinal, component in enumerate(project.components, start=1):
        original = component.refdes
        base = sanitize_refdes(original)
        candidate = base
        suffix_idx = 2
        while candidate in used:
            candidate = f"{base}_{suffix_idx}"
            suffix_idx += 1
        mapping.setdefault(original, candidate)
        mapping[component_instance_key(component, ordinal)] = candidate
        used.add(candidate)
    return mapping


def resolve_component_refdes(component: Any, refdes_map: dict[str, str]) -> str:
    source_id = str(getattr(component, "source_instance_id", "") or "").strip()
    if source_id:
        keyed = refdes_map.get(f"{component.refdes}::{source_id}")
        if keyed:
            return keyed
    return refdes_map.get(component.refdes, sanitize_refdes(component.refdes))
