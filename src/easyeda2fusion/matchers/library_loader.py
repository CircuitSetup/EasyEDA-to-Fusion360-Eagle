from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from easyeda2fusion.matchers.library_matcher import LibraryEntry
from easyeda2fusion.utils.io import load_json


DEFAULT_GENERIC_LIBRARY: list[LibraryEntry] = [
    LibraryEntry(
        device_name="GENERIC_R_0402",
        package_name="0402",
        symbol_name="R",
        component_class="resistor",
        aliases=["RES_0402"],
    ),
    LibraryEntry(
        device_name="GENERIC_C_0402",
        package_name="0402",
        symbol_name="C",
        component_class="capacitor",
        aliases=["CAP_0402"],
    ),
    LibraryEntry(
        device_name="GENERIC_R_0603",
        package_name="0603",
        symbol_name="R",
        component_class="resistor",
    ),
    LibraryEntry(
        device_name="GENERIC_C_0603",
        package_name="0603",
        symbol_name="C",
        component_class="capacitor",
    ),
    LibraryEntry(
        device_name="GENERIC_LED_0603",
        package_name="0603",
        symbol_name="LED",
        component_class="led",
    ),
    LibraryEntry(
        device_name="GENERIC_DIODE_SOT23",
        package_name="SOT23",
        symbol_name="D",
        component_class="diode",
    ),
    LibraryEntry(
        device_name="GENERIC_CONN_2PIN_2.54",
        package_name="HDR-2.54-1X02",
        symbol_name="CONN2",
        component_class="connector",
    ),
]


def load_library_entries(
    path: Path | None,
    *,
    resistor_library_path: Path | None = None,
    capacitor_library_path: Path | None = None,
    use_default_fusion_libraries: bool = True,
) -> list[LibraryEntry]:
    entries: list[LibraryEntry] = list(DEFAULT_GENERIC_LIBRARY)

    if use_default_fusion_libraries:
        for auto_dir in _default_fusion_library_dirs():
            if auto_dir.exists():
                entries.extend(_entries_from_lbr_dir(auto_dir))

    if resistor_library_path is not None:
        entries.extend(_entries_from_path(resistor_library_path))
    if capacitor_library_path is not None:
        entries.extend(_entries_from_path(capacitor_library_path))
    if path is None:
        return _dedupe_entries(entries)

    entries.extend(_entries_from_path(path))

    return _dedupe_entries(entries)


def _entries_from_path(path: Path) -> list[LibraryEntry]:
    entries: list[LibraryEntry] = []
    candidate = Path(path).expanduser()
    if not candidate.exists():
        return entries

    if candidate.is_file():
        suffix = candidate.suffix.lower()
        if suffix == ".json":
            entries.extend(_entries_from_file(candidate))
        elif suffix == ".lbr":
            entries.extend(_entries_from_lbr_file(candidate))
        return entries

    if candidate.is_dir():
        entries.extend(_entries_from_lbr_dir(candidate))
        for item in sorted(candidate.rglob("*.json")):
            entries.extend(_entries_from_file(item))
    return entries


def _entries_from_file(path: Path) -> list[LibraryEntry]:
    try:
        payload = load_json(path)
    except Exception:
        return []
    rows: list[dict[str, Any]]

    if isinstance(payload, list):
        rows = [row for row in payload if isinstance(row, dict)]
    elif isinstance(payload, dict) and isinstance(payload.get("entries"), list):
        rows = [row for row in payload["entries"] if isinstance(row, dict)]
    else:
        return []

    entries: list[LibraryEntry] = []
    for row in rows:
        entries.append(
            LibraryEntry(
                device_name=str(row.get("device_name") or row.get("device") or ""),
                package_name=str(row.get("package_name") or row.get("package") or ""),
                symbol_name=str(row.get("symbol_name") or row.get("symbol") or ""),
                mpn=str(row["mpn"]) if row.get("mpn") else None,
                aliases=[str(alias) for alias in row.get("aliases", []) if isinstance(alias, (str, int))],
                component_class=str(row["component_class"]) if row.get("component_class") else None,
                library_name=str(row["library_name"]) if row.get("library_name") else None,
                add_token=str(row["add_token"]) if row.get("add_token") else None,
                library_path=str(row["library_path"]) if row.get("library_path") else None,
            )
        )
    return [entry for entry in entries if entry.device_name and entry.package_name]


def _entries_from_lbr_dir(path: Path) -> list[LibraryEntry]:
    entries: list[LibraryEntry] = []
    for lbr in sorted(path.rglob("*.lbr")):
        entries.extend(_entries_from_lbr_file(lbr))
    return entries


def _entries_from_lbr_file(path: Path) -> list[LibraryEntry]:
    entries: list[LibraryEntry] = []
    lib_name = path.stem

    try:
        tree = ET.parse(path)
    except Exception:
        return entries

    root = tree.getroot()
    for deviceset in root.findall(".//library/devicesets/deviceset"):
        ds_name = str(deviceset.get("name") or "").strip()
        if not ds_name:
            continue

        for device in deviceset.findall("./devices/device"):
            package = str(device.get("package") or "").strip()
            if not package:
                continue

            dev_variant = str(device.get("name") or "").strip()
            full_name = f"{ds_name}{dev_variant}" if dev_variant else ds_name
            comp_class = _infer_component_class(f"{ds_name} {package}")
            mpn_candidates = _extract_device_mpn_candidates(device)
            aliases = [ds_name]
            aliases.extend(mpn_candidates)
            entries.append(
                LibraryEntry(
                    device_name=full_name,
                    package_name=package,
                    symbol_name=ds_name,
                    aliases=_dedupe_aliases(aliases),
                    component_class=comp_class,
                    library_name=lib_name,
                    add_token=f"{lib_name}:{full_name}",
                    library_path=str(path.resolve()),
                    mpn=mpn_candidates[0] if mpn_candidates else None,
                )
            )

    return entries


def _default_fusion_library_dirs() -> list[Path]:
    user = Path(os.environ.get("USERPROFILE", "~")).expanduser()
    appdata = Path(os.environ.get("APPDATA", "~")).expanduser()
    localapp = Path(os.environ.get("LOCALAPPDATA", "~")).expanduser()
    return [
        user / "Documents" / "EAGLE" / "lbr",
        user / "Documents" / "Autodesk" / "EAGLE" / "lbr",
        appdata / "Autodesk" / "EAGLE" / "lbr",
        localapp / "Autodesk" / "EAGLE" / "lbr",
        localapp / "Autodesk" / "Autodesk Fusion 360" / "Electron" / "lbr",
    ]


def _infer_component_class(text: str) -> str | None:
    raw = str(text or "").upper()
    token = _norm(raw)

    if any(item in raw for item in ("MOSFET", "PMOS", "NMOS")):
        return "mosfet"
    if any(item in raw for item in ("TRANSISTOR", " BJT ", " NPN ", " PNP ")):
        return "transistor"
    if re.search(r"\b2N7002\b|\bSI\d{4}[A-Z0-9\-]*\b|\bAO\d{4}[A-Z0-9\-]*\b|\bBSS\d+\b|\bFDN\d+\b", raw):
        return "mosfet"
    if re.search(r"\bS8050\b|\bS8550\b|\bMMBT\d+\b|\b2N\d+\b|\bBC\d+\b", raw):
        return "transistor"
    if "RELAY" in raw:
        return "relay"
    if "LED" in raw:
        return "led"
    if "DIODE" in raw or any(item in raw for item in ("TVS", "SCHOTTKY", "SOD-", "DO-214", "SMA", "SMB", "SMC")):
        return "diode"
    if "CONN" in raw or "HEADER" in raw:
        return "connector"
    if "INDUCTOR" in raw or "IND " in raw:
        return "inductor"
    if "CAP" in raw or re.search(r"\bC[-_ ]?(US|EU)?C?\d{4}\b", raw) or token.startswith("C0"):
        return "capacitor"
    if "RES" in raw or re.search(r"\bR[-_ ]?(US|EU)?[-_ ]?\d{4}\b", raw) or token.startswith("R0"):
        return "resistor"
    if token.startswith("D"):
        return "diode"
    if token.startswith("J"):
        return "connector"
    if token.startswith("U") or "IC" in token:
        return "ic"
    return None


def _extract_device_mpn_candidates(device: ET.Element) -> list[str]:
    values: list[str] = []
    for attribute in device.findall("./attribute"):
        name = str(attribute.get("name") or "")
        value = str(attribute.get("value") or "").strip()
        if _is_mpn_attribute(name) and value:
            values.append(value)

    for technology in device.findall("./technologies/technology"):
        for attribute in technology.findall("./attribute"):
            name = str(attribute.get("name") or "")
            value = str(attribute.get("value") or "").strip()
            if _is_mpn_attribute(name) and value:
                values.append(value)
    return _dedupe_aliases(values)


def _is_mpn_attribute(name: str) -> bool:
    key = _norm(name)
    return key in {
        "MPN",
        "PARTNUMBER",
        "PARTNO",
        "PARTNUM",
        "MFPN",
        "MFRPARTNUMBER",
        "MFRPARTNO",
        "MANUFACTURERPARTNUMBER",
        "MANUFACTURERPART",
    }


def _dedupe_aliases(values: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for raw in values:
        text = str(raw or "").strip()
        key = _norm(text)
        if not text or not key or key in seen:
            continue
        seen.add(key)
        unique.append(text)
    return unique


def _dedupe_entries(entries: list[LibraryEntry]) -> list[LibraryEntry]:
    unique: list[LibraryEntry] = []
    seen: set[tuple[str, str, str, str]] = set()
    for entry in entries:
        key = (
            _norm(entry.add_token or entry.device_name),
            _norm(entry.package_name),
            _norm(entry.library_name or ""),
            _norm(entry.library_path or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique


def _norm(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(text or "").upper())
