from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from pathlib import Path


def parse_xml_root_with_entity_sanitization(path: Path) -> ET.Element | None:
    try:
        return ET.parse(path).getroot()
    except Exception:
        pass

    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None

    sanitized = sanitize_xml_named_entities(raw)
    try:
        return ET.fromstring(sanitized)
    except Exception:
        return None


def sanitize_xml_named_entities(text: str) -> str:
    xml_builtins = {"amp", "lt", "gt", "apos", "quot"}
    pattern = re.compile(r"&([A-Za-z][A-Za-z0-9]+);")

    def _replace(match: re.Match[str]) -> str:
        name = str(match.group(1) or "")
        if name in xml_builtins:
            return match.group(0)
        decoded = html.unescape(match.group(0))
        if decoded == match.group(0):
            return ""
        return decoded

    return pattern.sub(_replace, text)
