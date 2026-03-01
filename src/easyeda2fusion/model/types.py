from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class SourceFormat(str, Enum):
    EASYEDA_STD = "easyeda_std"
    EASYEDA_PRO = "easyeda_pro"
    UNKNOWN = "unknown"


class ConversionMode(str, Enum):
    FULL = "full"
    SCHEMATIC_ONLY = "schematic"
    BOARD_ONLY = "board"
    BOARD_INFER_SCHEMATIC = "board-infer-schematic"


class MatchMode(str, Enum):
    AUTO = "auto"
    PROMPT = "prompt"
    STRICT = "strict"
    PACKAGE_FIRST = "package-first"


class Side(str, Enum):
    TOP = "top"
    BOTTOM = "bottom"
    INTERNAL = "internal"
    UNKNOWN = "unknown"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass
class ConversionEvent:
    severity: Severity
    code: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class Point:
    x_mm: float
    y_mm: float


@dataclass
class Bounds:
    min_x_mm: float
    min_y_mm: float
    max_x_mm: float
    max_y_mm: float


@dataclass
class Layer:
    source_id: str
    source_name: str
    family: str
    mapped_name: str
    category: str
    copper_index: int | None = None
    lossy: bool = False


@dataclass
class Rule:
    name: str
    value: str
    description: str | None = None


@dataclass
class TextItem:
    text: str
    at: Point
    layer: str
    size_mm: float
    rotation_deg: float = 0.0
    mirrored: bool = False


@dataclass
class SymbolPin:
    pin_number: str
    pin_name: str
    at: Point


@dataclass
class Symbol:
    symbol_id: str
    name: str
    pins: list[SymbolPin] = field(default_factory=list)
    graphics: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Pad:
    pad_number: str
    at: Point
    shape: str
    width_mm: float
    height_mm: float
    drill_mm: float | None = None
    layer: str = "top_copper"
    rotation_deg: float = 0.0
    net: str | None = None


@dataclass
class Package:
    package_id: str
    name: str
    pads: list[Pad] = field(default_factory=list)
    outline: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Device:
    device_id: str
    name: str
    symbol_id: str
    package_id: str
    pin_pad_map: dict[str, str] = field(default_factory=dict)


@dataclass
class Component:
    refdes: str
    value: str
    source_name: str
    source_instance_id: str | None = None
    symbol_id: str | None = None
    package_id: str | None = None
    device_id: str | None = None
    manufacturer: str | None = None
    mpn: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    at: Point = field(default_factory=lambda: Point(0.0, 0.0))
    rotation_deg: float = 0.0
    side: Side = Side.TOP
    sheet_id: str | None = None


@dataclass
class NetNode:
    refdes: str
    pin: str


@dataclass
class Net:
    name: str
    nodes: list[NetNode] = field(default_factory=list)


@dataclass
class SchematicSheet:
    sheet_id: str
    name: str
    components: list[str] = field(default_factory=list)
    wires: list[dict[str, Any]] = field(default_factory=list)
    labels: list[TextItem] = field(default_factory=list)
    ports: list[dict[str, Any]] = field(default_factory=list)
    junctions: list[Point] = field(default_factory=list)
    no_connects: list[Point] = field(default_factory=list)
    annotations: list[TextItem] = field(default_factory=list)


@dataclass
class Track:
    start: Point
    end: Point
    width_mm: float
    layer: str
    net: str | None = None


@dataclass
class Via:
    at: Point
    drill_mm: float
    diameter_mm: float
    net: str | None = None
    start_layer: str = "top_copper"
    end_layer: str = "bottom_copper"


@dataclass
class Arc:
    start: Point
    end: Point
    center: Point
    width_mm: float
    layer: str
    net: str | None = None


@dataclass
class Region:
    region_id: str
    layer: str
    points: list[Point]
    net: str | None = None
    kind: str = "polygon"


@dataclass
class Hole:
    at: Point
    drill_mm: float
    plated: bool


@dataclass
class Board:
    layers: list[Layer] = field(default_factory=list)
    outline: list[Region] = field(default_factory=list)
    cutouts: list[Region] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)
    vias: list[Via] = field(default_factory=list)
    pads: list[Pad] = field(default_factory=list)
    arcs: list[Arc] = field(default_factory=list)
    regions: list[Region] = field(default_factory=list)
    keepouts: list[Region] = field(default_factory=list)
    mechanical: list[dict[str, Any]] = field(default_factory=list)
    text: list[TextItem] = field(default_factory=list)
    holes: list[Hole] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class LibraryMatch:
    refdes: str
    stage: str
    matched: bool
    target_device: str | None = None
    target_package: str | None = None
    reason: str | None = None
    candidates: list[str] = field(default_factory=list)
    created_new_part: bool = False


@dataclass
class Project:
    project_id: str
    name: str
    source_format: SourceFormat
    input_files: list[str]
    sheets: list[SchematicSheet] = field(default_factory=list)
    components: list[Component] = field(default_factory=list)
    symbols: list[Symbol] = field(default_factory=list)
    packages: list[Package] = field(default_factory=list)
    devices: list[Device] = field(default_factory=list)
    nets: list[Net] = field(default_factory=list)
    board: Board | None = None
    layers: list[Layer] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    events: list[ConversionEvent] = field(default_factory=list)
    library_matches: list[LibraryMatch] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ParsedDocument:
    doc_type: str
    name: str
    raw_objects: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedSource:
    source_format: SourceFormat
    input_files: list[str]
    documents: list[ParsedDocument]
    layers: list[dict[str, Any]] = field(default_factory=list)
    rules: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    events: list[ConversionEvent] = field(default_factory=list)


def project_event(
    severity: Severity,
    code: str,
    message: str,
    context: dict[str, Any] | None = None,
) -> ConversionEvent:
    return ConversionEvent(
        severity=severity,
        code=code,
        message=message,
        context=context or {},
    )
