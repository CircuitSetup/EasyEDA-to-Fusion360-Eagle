from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from easyeda2fusion.model import Component, Device, Package, Pad, Point, Symbol, SymbolPin


PASSIVE_PACKAGE_DIMENSIONS_MM: dict[str, tuple[float, float]] = {
    "0201": (0.6, 0.3),
    "0402": (1.0, 0.5),
    "0603": (1.6, 0.8),
    "0805": (2.0, 1.25),
    "1206": (3.2, 1.6),
}


@dataclass
class GeneratedLibraryPart:
    symbol: Symbol
    package: Package
    device: Device
    source: str


class LibraryBuilder:
    """Creates missing library symbol/package/device entries conservatively.

    The builder only generates entries when the source data supports safe geometry.
    """

    def __init__(self) -> None:
        self._symbol_defs: dict[str, dict[str, Any]] = {}
        self._device_id_to_symbol_id: dict[str, str] = {}
        self._device_name_to_symbol_id: dict[str, str] = {}

    def configure(self, metadata: dict[str, Any]) -> None:
        symbol_defs = metadata.get("symbol_defs")
        if isinstance(symbol_defs, dict):
            self._symbol_defs = {
                str(key): value
                for key, value in symbol_defs.items()
                if isinstance(value, dict)
            }
        else:
            self._symbol_defs = {}

        device_id_to_symbol = metadata.get("device_id_to_symbol_id")
        if isinstance(device_id_to_symbol, dict):
            self._device_id_to_symbol_id = {
                str(key): str(value)
                for key, value in device_id_to_symbol.items()
                if str(key).strip() and str(value).strip()
            }
        else:
            self._device_id_to_symbol_id = {}

        device_name_to_symbol = metadata.get("device_name_to_symbol_id")
        if isinstance(device_name_to_symbol, dict):
            self._device_name_to_symbol_id = {
                _canon_key(str(key)): str(value)
                for key, value in device_name_to_symbol.items()
                if str(key).strip() and str(value).strip()
            }
        else:
            self._device_name_to_symbol_id = {}

    def synthesize_missing_part(
        self,
        component: Component,
        package_lookup: dict[str, Package],
        pin_net_hints: dict[str, set[str]] | None = None,
    ) -> tuple[GeneratedLibraryPart | None, str | None]:
        package_name = (component.package_id or component.attributes.get("package_name") or "").strip()
        if not package_name:
            return None, "missing_package_identifier"

        existing = package_lookup.get(package_name)
        if existing is not None:
            symbol = self._symbol_for_component_and_package(
                component,
                existing,
                pin_net_hints=pin_net_hints or {},
            )
            device = Device(
                device_id=f"DEV_{component.refdes}",
                name=f"{component.source_name}_DEV",
                symbol_id=symbol.symbol_id,
                package_id=existing.package_id,
                pin_pad_map=self._default_pin_pad_map(symbol, existing),
            )
            return (
                GeneratedLibraryPart(symbol=symbol, package=existing, device=device, source="source_package"),
                None,
            )

        generic_code = self._extract_package_code(package_name)
        if generic_code in PASSIVE_PACKAGE_DIMENSIONS_MM and self._is_passive(component):
            pkg = self._build_generic_passive_package(package_name, generic_code)
            symbol = self._symbol_for_component(component, pin_net_hints=pin_net_hints or {})
            device = Device(
                device_id=f"DEV_{component.refdes}",
                name=f"{component.source_name}_DEV",
                symbol_id=symbol.symbol_id,
                package_id=pkg.package_id,
                pin_pad_map=self._default_pin_pad_map(symbol, pkg),
            )
            return (
                GeneratedLibraryPart(symbol=symbol, package=pkg, device=device, source="generic_passive"),
                None,
            )

        return None, "insufficient_package_geometry"

    @staticmethod
    def _is_passive(component: Component) -> bool:
        if _is_resistor_array_component(component):
            return False
        ref = component.refdes.upper()
        return ref.startswith(("R", "C", "L", "FB", "D", "LED"))

    @staticmethod
    def _extract_package_code(package_name: str) -> str:
        for code in PASSIVE_PACKAGE_DIMENSIONS_MM:
            if code in package_name:
                return code
        return package_name

    def _symbol_for_component(self, component: Component, pin_net_hints: dict[str, set[str]]) -> Symbol:
        pin_names = ["1", "2"] if self._is_passive(component) else ["1"]
        used_pin_names: set[str] = set()
        pins = []
        allow_net_hint = True
        for idx, pin in enumerate(pin_names):
            pin_name = _resolve_pin_label(
                pad_number=pin,
                pin_meta=None,
                pin_net_hints=pin_net_hints.get(pin, set()),
                used_pin_names=used_pin_names,
                allow_net_hint=allow_net_hint,
            )
            pins.append(SymbolPin(pin_number=pin, pin_name=pin_name, at=Point(x_mm=float(idx) * 2.54, y_mm=0.0)))
        return Symbol(symbol_id=f"SYM_{component.refdes}", name=component.source_name, pins=pins)

    def _symbol_for_component_and_package(
        self,
        component: Component,
        package: Package,
        pin_net_hints: dict[str, set[str]],
    ) -> Symbol:
        if not package.pads:
            return self._symbol_for_component(component, pin_net_hints=pin_net_hints)

        pad_numbers = []
        seen = set()
        for pad in package.pads:
            num = str(pad.pad_number).strip()
            if not num or num in seen:
                continue
            seen.add(num)
            pad_numbers.append(num)

        if not pad_numbers:
            return self._symbol_for_component(component, pin_net_hints=pin_net_hints)

        pad_numbers = sorted(pad_numbers, key=_pin_sort_key)
        pin_meta_by_number = self._pin_metadata_for_component(component, package)
        used_pin_names: set[str] = set()

        if len(pad_numbers) <= 2:
            pins = []
            allow_net_hint = True
            for idx, pad_num in enumerate(pad_numbers):
                pin_name = _resolve_pin_label(
                    pad_number=pad_num,
                    pin_meta=pin_meta_by_number.get(pad_num),
                    pin_net_hints=pin_net_hints.get(pad_num, set()),
                    used_pin_names=used_pin_names,
                    allow_net_hint=allow_net_hint,
                )
                pins.append(SymbolPin(pin_number=pad_num, pin_name=pin_name, at=Point(x_mm=float(idx) * 5.08, y_mm=0.0)))
            return Symbol(symbol_id=f"SYM_{component.refdes}", name=component.source_name, pins=pins)

        if _is_resistor_array_component(component):
            return self._resistor_array_symbol(
                component=component,
                pad_numbers=pad_numbers,
                pin_meta_by_number=pin_meta_by_number,
                pin_net_hints=pin_net_hints,
                used_pin_names=used_pin_names,
            )

        left_count = (len(pad_numbers) + 1) // 2
        right_count = len(pad_numbers) - left_count
        row_count = max(left_count, right_count)
        pin_pitch = 2.54
        body_half_w = 5.08
        body_half_h = max(3.81, (row_count - 1) * pin_pitch / 2.0 + 1.27)

        pins: list[SymbolPin] = []
        left_y_start = body_half_h
        for idx, pad_num in enumerate(pad_numbers[:left_count]):
            y = left_y_start - idx * pin_pitch
            pin_name = _resolve_pin_label(
                pad_number=pad_num,
                pin_meta=pin_meta_by_number.get(pad_num),
                pin_net_hints=pin_net_hints.get(pad_num, set()),
                used_pin_names=used_pin_names,
                allow_net_hint=True,
            )
            pins.append(SymbolPin(pin_number=pad_num, pin_name=pin_name, at=Point(x_mm=-body_half_w, y_mm=y)))

        right_y_start = body_half_h
        for idx, pad_num in enumerate(pad_numbers[left_count:]):
            y = right_y_start - idx * pin_pitch
            pin_name = _resolve_pin_label(
                pad_number=pad_num,
                pin_meta=pin_meta_by_number.get(pad_num),
                pin_net_hints=pin_net_hints.get(pad_num, set()),
                used_pin_names=used_pin_names,
                allow_net_hint=True,
            )
            pins.append(SymbolPin(pin_number=pad_num, pin_name=pin_name, at=Point(x_mm=body_half_w, y_mm=y)))

        graphics = [
            {"kind": "wire", "x1_mm": -body_half_w, "y1_mm": -body_half_h, "x2_mm": body_half_w, "y2_mm": -body_half_h},
            {"kind": "wire", "x1_mm": body_half_w, "y1_mm": -body_half_h, "x2_mm": body_half_w, "y2_mm": body_half_h},
            {"kind": "wire", "x1_mm": body_half_w, "y1_mm": body_half_h, "x2_mm": -body_half_w, "y2_mm": body_half_h},
            {"kind": "wire", "x1_mm": -body_half_w, "y1_mm": body_half_h, "x2_mm": -body_half_w, "y2_mm": -body_half_h},
        ]
        return Symbol(
            symbol_id=f"SYM_{component.refdes}",
            name=component.source_name,
            pins=pins,
            graphics=graphics,
        )

    def _resistor_array_symbol(
        self,
        component: Component,
        pad_numbers: list[str],
        pin_meta_by_number: dict[str, dict[str, str]],
        pin_net_hints: dict[str, set[str]],
        used_pin_names: set[str],
    ) -> Symbol:
        left_count = (len(pad_numbers) + 1) // 2
        right_count = len(pad_numbers) - left_count
        row_count = max(left_count, right_count)
        pin_pitch = 2.54
        body_half_w = 6.35
        body_half_h = max(3.81, (row_count - 1) * pin_pitch / 2.0 + 1.27)

        left_pads = pad_numbers[:left_count]
        right_pads = pad_numbers[left_count:]

        pins: list[SymbolPin] = []
        left_y_start = body_half_h
        left_y_positions: list[float] = []
        for idx, pad_num in enumerate(left_pads):
            y = left_y_start - idx * pin_pitch
            left_y_positions.append(y)
            pin_name = _resolve_pin_label(
                pad_number=pad_num,
                pin_meta=pin_meta_by_number.get(pad_num),
                pin_net_hints=pin_net_hints.get(pad_num, set()),
                used_pin_names=used_pin_names,
                allow_net_hint=True,
            )
            pins.append(SymbolPin(pin_number=pad_num, pin_name=pin_name, at=Point(x_mm=-body_half_w, y_mm=y)))

        right_y_start = body_half_h
        right_y_positions: list[float] = []
        for idx, pad_num in enumerate(right_pads):
            y = right_y_start - idx * pin_pitch
            right_y_positions.append(y)
            pin_name = _resolve_pin_label(
                pad_number=pad_num,
                pin_meta=pin_meta_by_number.get(pad_num),
                pin_net_hints=pin_net_hints.get(pad_num, set()),
                used_pin_names=used_pin_names,
                allow_net_hint=True,
            )
            pins.append(SymbolPin(pin_number=pad_num, pin_name=pin_name, at=Point(x_mm=body_half_w, y_mm=y)))

        graphics = [
            {"kind": "wire", "x1_mm": -body_half_w, "y1_mm": -body_half_h, "x2_mm": body_half_w, "y2_mm": -body_half_h},
            {"kind": "wire", "x1_mm": body_half_w, "y1_mm": -body_half_h, "x2_mm": body_half_w, "y2_mm": body_half_h},
            {"kind": "wire", "x1_mm": body_half_w, "y1_mm": body_half_h, "x2_mm": -body_half_w, "y2_mm": body_half_h},
            {"kind": "wire", "x1_mm": -body_half_w, "y1_mm": body_half_h, "x2_mm": -body_half_w, "y2_mm": -body_half_h},
        ]

        pair_count = min(len(left_y_positions), len(right_y_positions))
        for idx in range(pair_count):
            y = (left_y_positions[idx] + right_y_positions[idx]) / 2.0
            start_x = -2.0
            step = 0.9
            points = [
                (start_x, y),
                (start_x + step, y + 0.5),
                (start_x + 2 * step, y - 0.5),
                (start_x + 3 * step, y + 0.5),
                (start_x + 4 * step, y - 0.5),
                (start_x + 5 * step, y),
            ]
            for left, right in zip(points, points[1:]):
                graphics.append(
                    {
                        "kind": "wire",
                        "x1_mm": left[0],
                        "y1_mm": left[1],
                        "x2_mm": right[0],
                        "y2_mm": right[1],
                    }
                )

        return Symbol(
            symbol_id=f"SYM_{component.refdes}",
            name=component.source_name,
            pins=pins,
            graphics=graphics,
        )

    def _pin_metadata_for_component(self, component: Component, package: Package) -> dict[str, dict[str, str]]:
        symbol_id = self._resolve_component_symbol_id(component)
        if not symbol_id:
            return {}
        symbol_def = self._symbol_defs.get(symbol_id)
        if not isinstance(symbol_def, dict):
            return {}
        raw_pins = symbol_def.get("pins")
        if not isinstance(raw_pins, list):
            return {}

        package_pads = {
            str(pad.pad_number).strip()
            for pad in package.pads
            if str(pad.pad_number).strip()
        }
        out: dict[str, dict[str, str]] = {}
        for pin in raw_pins:
            if not isinstance(pin, dict):
                continue
            number = str(pin.get("number") or "").strip()
            if not number:
                continue
            if package_pads and number not in package_pads:
                continue
            out[number] = {
                "name": str(pin.get("name") or number).strip() or number,
                "pin_type": str(pin.get("pin_type") or "").strip(),
            }
        return out

    def _resolve_component_symbol_id(self, component: Component) -> str | None:
        attrs = component.attributes if isinstance(component.attributes, dict) else {}
        explicit = str(attrs.get("Symbol") or "").strip()
        if explicit and explicit in self._symbol_defs:
            return explicit

        device_id = str(attrs.get("Device") or "").strip()
        if device_id:
            mapped = self._device_id_to_symbol_id.get(device_id)
            if mapped and mapped in self._symbol_defs:
                return mapped

        for raw_name in (
            component.source_name,
            attrs.get("Name"),
            attrs.get("Device"),
        ):
            key = _canon_key(raw_name)
            if not key:
                continue
            mapped = self._device_name_to_symbol_id.get(key)
            if mapped and mapped in self._symbol_defs:
                return mapped
        return None

    @staticmethod
    def _build_generic_passive_package(package_name: str, code: str) -> Package:
        body_w, body_h = PASSIVE_PACKAGE_DIMENSIONS_MM[code]
        pad_w = max(0.35, body_w * 0.35)
        pad_h = max(0.35, body_h * 0.8)
        pitch = body_w + pad_w
        pads = [
            Pad(
                pad_number="1",
                at=Point(x_mm=-pitch / 2.0, y_mm=0.0),
                shape="rect",
                width_mm=pad_w,
                height_mm=pad_h,
                layer="top_copper",
            ),
            Pad(
                pad_number="2",
                at=Point(x_mm=pitch / 2.0, y_mm=0.0),
                shape="rect",
                width_mm=pad_w,
                height_mm=pad_h,
                layer="top_copper",
            ),
        ]
        return Package(package_id=package_name, name=package_name, pads=pads)

    @staticmethod
    def _default_pin_pad_map(symbol: Symbol, package: Package) -> dict[str, str]:
        package_pads = [str(pad.pad_number) for pad in package.pads]
        mapping: dict[str, str] = {}
        for pin in symbol.pins:
            pin_number = str(pin.pin_number).strip()
            pin_key = str(pin.pin_name or pin.pin_number).strip()
            if not pin_number or not pin_key:
                continue
            if pin_number in package_pads:
                mapping[pin_key] = pin_number
        if not mapping and symbol.pins and package_pads:
            for idx, pin in enumerate(symbol.pins):
                if idx < len(package_pads):
                    pin_key = str(pin.pin_name or pin.pin_number).strip()
                    if pin_key:
                        mapping[pin_key] = package_pads[idx]
        return mapping


def _pin_sort_key(pin: str) -> tuple[int, int, str]:
    text = str(pin or "").strip()
    if text.isdigit():
        return (0, int(text), "")
    match = re.match(r"^([A-Za-z]+)(\d+)$", text)
    if match:
        return (1, int(match.group(2)), match.group(1))
    return (2, 0, text)


def _resolve_pin_label(
    pad_number: str,
    pin_meta: dict[str, str] | None,
    pin_net_hints: set[str],
    used_pin_names: set[str],
    allow_net_hint: bool,
) -> str:
    base = str(pad_number).strip() or "PIN"
    if pin_meta:
        meta_name = str(pin_meta.get("name") or "").strip()
        if meta_name and not _is_generic_pin_name(meta_name, pad_number):
            base = meta_name

    if allow_net_hint and _is_generic_pin_name(base, pad_number):
        net_hint = _preferred_net_pin_name(pin_net_hints)
        if net_hint:
            base = net_hint

    safe = _sanitize_pin_name(base)
    if not safe:
        safe = str(pad_number).strip() or "PIN"

    candidate = safe
    if candidate in used_pin_names and str(pad_number).strip():
        candidate = _sanitize_pin_name(f"{safe}_{str(pad_number).strip()}")
    idx = 2
    while candidate in used_pin_names:
        candidate = f"{safe}_{idx}"
        idx += 1
    used_pin_names.add(candidate)
    return candidate


def _is_generic_pin_name(pin_name: str, pad_number: str) -> bool:
    name = str(pin_name or "").strip().upper()
    pad = str(pad_number or "").strip().upper()
    if not name:
        return True
    if name == pad:
        return True
    if re.fullmatch(r"PIN\d+", name):
        return True
    if re.fullmatch(r"P\d+", name):
        return True
    return False


def _single_power_net_name(names: set[str]) -> str | None:
    cleaned = [str(item or "").strip() for item in names if str(item or "").strip()]
    if not cleaned:
        return None
    power = [item for item in cleaned if _is_power_net(item)]
    if len(power) != 1:
        return None
    return power[0]


def _preferred_net_pin_name(names: set[str]) -> str | None:
    cleaned = [
        str(item or "").strip()
        for item in names
        if str(item or "").strip()
        and not _is_anonymous_net_name(str(item or "").strip())
    ]
    if not cleaned:
        return None

    # Single clear net hint is the strongest label for inferred symbols.
    unique = sorted({name for name in cleaned}, key=lambda value: (len(value), value.upper()))
    if len(unique) == 1:
        return unique[0]

    # For multi-net hints, prefer canonical power rails.
    power_hint = _single_power_net_name(set(unique))
    if power_hint:
        return power_hint
    return None


def _is_anonymous_net_name(name: str) -> bool:
    token = _canon_key(name)
    return token.startswith("N") and "$" in str(name)


def _is_power_net(name: str) -> bool:
    token = _canon_key(name)
    if not token:
        return False
    power_tokens = {
        "GND",
        "AGND",
        "DGND",
        "PGND",
        "SGND",
        "VCC",
        "VDD",
        "VSS",
        "VIN",
        "VBAT",
        "VOUT",
        "AVDD",
        "DVDD",
        "AVSS",
        "DVSS",
        "3V3",
        "5V",
        "5V0",
        "12V",
        "24V",
    }
    return token in power_tokens or token.startswith("VDD") or token.startswith("VCC")


def _sanitize_pin_name(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^A-Za-z0-9_+\-]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text


def _canon_key(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _is_resistor_array_component(component: Component) -> bool:
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
    if "RES-ARRAY" in blob or "RESISTORARRAY" in blob or "RESISTOR NETWORK" in blob:
        return True
    if re.search(r"\bRN[-_ ]?\d+\b", blob):
        return True
    return ref.startswith(("RN", "RA"))
