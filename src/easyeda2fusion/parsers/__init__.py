from __future__ import annotations

from pathlib import Path

from easyeda2fusion.model import ParsedSource, SourceFormat
from easyeda2fusion.parsers.base import ParseError
from easyeda2fusion.parsers.easyeda_pro import EasyEDAProParser
from easyeda2fusion.parsers.easyeda_std import EasyEDAStdParser
from easyeda2fusion.utils.io import load_json


def detect_source_format(paths: list[Path]) -> SourceFormat:
    if not paths:
        return SourceFormat.UNKNOWN

    payload = load_json(paths[0])
    if not isinstance(payload, dict):
        return SourceFormat.UNKNOWN

    std = EasyEDAStdParser().can_parse(payload)
    pro = EasyEDAProParser().can_parse(payload)

    if std and not pro:
        return SourceFormat.EASYEDA_STD
    if pro and not std:
        return SourceFormat.EASYEDA_PRO
    if std and pro:
        fmt = str(payload.get("format", "")).lower()
        if "pro" in fmt:
            return SourceFormat.EASYEDA_PRO
        return SourceFormat.EASYEDA_STD
    return SourceFormat.UNKNOWN


def parse_easyeda_files(paths: list[Path], forced_format: SourceFormat | None = None) -> ParsedSource:
    if not paths:
        raise ParseError("No input files provided")

    source_format = forced_format or detect_source_format(paths)

    if source_format == SourceFormat.EASYEDA_STD:
        return EasyEDAStdParser().parse_files(paths)
    if source_format == SourceFormat.EASYEDA_PRO:
        return EasyEDAProParser().parse_files(paths)

    raise ParseError(
        "Unable to detect EasyEDA source format. Use --source-format to force easyeda_std or easyeda_pro."
    )
