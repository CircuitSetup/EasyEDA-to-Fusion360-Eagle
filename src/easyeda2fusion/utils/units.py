from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from easyeda2fusion.model import SourceFormat

MIL_TO_MM = 0.0254
INCH_TO_MM = 25.4


@dataclass
class UnitConfig:
    source_format: SourceFormat
    coordinate_scale_to_mm: float
    y_axis_inverted: bool = False
    origin_x_mm: float = 0.0
    origin_y_mm: float = 0.0


class UnitNormalizer:
    """Normalizes source coordinates to millimeters.

    EasyEDA file families use different coordinate scales and potentially different
    axis conventions. This helper centralizes the transformation.
    """

    def __init__(self, cfg: UnitConfig) -> None:
        self.cfg = cfg

    @staticmethod
    def from_metadata(source_format: SourceFormat, metadata: dict[str, Any]) -> UnitConfig:
        unit_name = str(metadata.get("unit", "")).lower().strip()
        explicit_scale = metadata.get("coordinate_scale_to_mm")
        y_axis_inverted = bool(metadata.get("y_axis_inverted", False))
        origin = metadata.get("origin", {}) if isinstance(metadata.get("origin"), dict) else {}
        origin_raw = metadata.get("origin_raw", {}) if isinstance(metadata.get("origin_raw"), dict) else {}

        if explicit_scale is not None:
            scale = float(explicit_scale)
        elif unit_name == "mm":
            scale = 1.0
        elif unit_name == "mil":
            scale = MIL_TO_MM
        elif unit_name in {"inch", "in"}:
            scale = INCH_TO_MM
        elif source_format == SourceFormat.EASYEDA_STD:
            # Standard/Lite files commonly use 10-mil internal units.
            scale = 10.0 * MIL_TO_MM
        elif source_format == SourceFormat.EASYEDA_PRO:
            # Pro files are typically metric-oriented in exported JSON.
            scale = 1.0
        else:
            scale = 1.0

        origin_x_mm = float(origin.get("x_mm", 0.0))
        origin_y_mm = float(origin.get("y_mm", 0.0))
        if not origin and origin_raw:
            # Legacy Standard shape-string exports often use raw CAD coordinates
            # anchored at head.x/head.y. Convert this raw origin with the resolved
            # unit scale so geometry lands near project origin.
            raw_x = float(origin_raw.get("x", 0.0))
            raw_y = float(origin_raw.get("y", 0.0))
            origin_x_mm = -raw_x * scale
            origin_y_mm = -raw_y * scale

        return UnitConfig(
            source_format=source_format,
            coordinate_scale_to_mm=scale,
            y_axis_inverted=y_axis_inverted,
            origin_x_mm=origin_x_mm,
            origin_y_mm=origin_y_mm,
        )

    def to_mm(self, x_raw: float | int, y_raw: float | int) -> tuple[float, float]:
        x = (float(x_raw) * self.cfg.coordinate_scale_to_mm) + self.cfg.origin_x_mm
        y = (float(y_raw) * self.cfg.coordinate_scale_to_mm) + self.cfg.origin_y_mm
        if self.cfg.y_axis_inverted:
            y = -y
        return (x, y)

    def scalar_to_mm(self, value_raw: float | int) -> float:
        return float(value_raw) * self.cfg.coordinate_scale_to_mm
