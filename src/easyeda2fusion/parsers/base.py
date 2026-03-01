from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from easyeda2fusion.model import ParsedSource, SourceFormat


class ParseError(RuntimeError):
    pass


class EasyEDAParser(ABC):
    source_format: SourceFormat

    @abstractmethod
    def can_parse(self, payload: dict[str, Any]) -> bool:
        raise NotImplementedError

    @abstractmethod
    def parse_files(self, paths: list[Path]) -> ParsedSource:
        raise NotImplementedError
