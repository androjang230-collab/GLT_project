"""Core validation helpers for safe RPG Maker translation application."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engines.rpgmaker.extractor import find_control_codes


_JSON_PATH_TOKEN = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]")
_HIRAGANA = re.compile(r"[ぁ-ゖ]")
_KATAKANA = re.compile(r"[ァ-ヺｦ-ﾟ]")
_CJK_KANJI = re.compile(r"[㐀-䶿一-鿿豈-﫿]")

JsonPathToken = str | int


class JsonPathError(ValueError):
    """Raised when a supported Phase 2 JSON path is invalid or unreachable."""


@dataclass(frozen=True, slots=True)
class ControlCodeDifference:
    missing: tuple[str, ...]
    added: tuple[str, ...]

    @property
    def matches(self) -> bool:
        return not self.missing and not self.added

    def describe(self) -> str:
        details: list[str] = []
        if self.missing:
            details.append(f"missing={list(self.missing)!r}")
        if self.added:
            details.append(f"added={list(self.added)!r}")
        return "; ".join(details) or "control codes match"


@dataclass(frozen=True, slots=True)
class JapaneseScriptPresence:
    hiragana: bool
    katakana: bool
    cjk_kanji: bool

    @property
    def kana(self) -> bool:
        return self.hiragana or self.katakana

    @property
    def labels(self) -> tuple[str, ...]:
        labels: list[str] = []
        if self.hiragana:
            labels.append("Hiragana")
        if self.katakana:
            labels.append("Katakana")
        if self.cjk_kanji:
            labels.append("CJK Kanji")
        return tuple(labels)


@dataclass(frozen=True, slots=True)
class JapaneseAllowlist:
    """Literal substring allowlist loaded from a portable UTF-8 text file."""

    entries: tuple[str, ...] = ()

    @classmethod
    def from_file(cls, path: Path) -> "JapaneseAllowlist":
        entries = tuple(
            line.strip()
            for line in path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        return cls(entries=entries)

    def allows(self, text: str) -> bool:
        return any(entry in text for entry in self.entries)


def parse_json_path(json_path: str) -> tuple[JsonPathToken, ...]:
    """Parse the exact dot/index JSON path syntax emitted in Phase 2."""

    if not isinstance(json_path, str) or not json_path.startswith("$"):
        raise JsonPathError("json_path must start with '$'")
    position = 1
    tokens: list[JsonPathToken] = []
    while position < len(json_path):
        match = _JSON_PATH_TOKEN.match(json_path, position)
        if match is None:
            raise JsonPathError(f"invalid json_path syntax at character {position}")
        key, index = match.groups()
        tokens.append(key if key is not None else int(index))
        position = match.end()
    if not tokens:
        raise JsonPathError("json_path must identify a value below the document root")
    return tuple(tokens)


def get_json_value(document: Any, tokens: tuple[JsonPathToken, ...]) -> Any:
    current = document
    for token in tokens:
        if isinstance(token, str):
            if not isinstance(current, dict) or token not in current:
                raise JsonPathError(f"object key does not exist: {token}")
            current = current[token]
        else:
            if not isinstance(current, list):
                raise JsonPathError(f"expected an array before index {token}")
            if not 0 <= token < len(current):
                raise JsonPathError(f"array index out of range: {token}")
            current = current[token]
    return current


def set_json_value(
    document: Any,
    tokens: tuple[JsonPathToken, ...],
    value: str,
) -> None:
    parent = document
    for token in tokens[:-1]:
        parent = get_json_value(parent, (token,))
    final = tokens[-1]
    if isinstance(final, str):
        if not isinstance(parent, dict) or final not in parent:
            raise JsonPathError(f"object key does not exist: {final}")
        parent[final] = value
    else:
        if not isinstance(parent, list) or not 0 <= final < len(parent):
            raise JsonPathError(f"array index out of range: {final}")
        parent[final] = value


def compare_control_codes(original: str, translation: str) -> ControlCodeDifference:
    """Compare control-code multisets, preserving value and duplicate counts."""

    original_codes = Counter(find_control_codes(original))
    translated_codes = Counter(find_control_codes(translation))
    missing = tuple(sorted((original_codes - translated_codes).elements()))
    added = tuple(sorted((translated_codes - original_codes).elements()))
    return ControlCodeDifference(missing=missing, added=added)


def contains_japanese_kana(text: str) -> bool:
    return detect_japanese_scripts(text).kana


def detect_japanese_scripts(text: str) -> JapaneseScriptPresence:
    return JapaneseScriptPresence(
        hiragana=_HIRAGANA.search(text) is not None,
        katakana=_KATAKANA.search(text) is not None,
        cjk_kanji=_CJK_KANJI.search(text) is not None,
    )


def find_unexpected_changes(
    before: Any,
    after: Any,
    allowed_paths: set[str],
) -> list[str]:
    """Return semantic changes outside approved translation string leaves."""

    unexpected: list[str] = []

    def compare(left: Any, right: Any, path: str) -> None:
        if type(left) is not type(right):
            unexpected.append(path)
            return
        if isinstance(left, dict):
            if left.keys() != right.keys():
                unexpected.append(path)
                return
            for key in left:
                compare(left[key], right[key], f"{path}.{key}")
            return
        if isinstance(left, list):
            if len(left) != len(right):
                unexpected.append(path)
                return
            for index, (left_item, right_item) in enumerate(zip(left, right)):
                compare(left_item, right_item, f"{path}[{index}]")
            return
        if left != right and path not in allowed_paths:
            unexpected.append(path)

    compare(before, after, "$")
    return unexpected
