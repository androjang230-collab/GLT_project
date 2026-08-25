"""Narrow, explicit RPG Maker plugin-command text rules.

This module deliberately does not guess that every plugin string is player
visible.  Verified rules may enter the translation pipeline; conditional and
internal observations remain audit-only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterator


VERIFIED = "VERIFIED_TRANSLATABLE"
CONDITIONAL = "CONDITIONAL_TRANSLATABLE"
INTERNAL = "INTERNAL"

MV_INFO_PREFIX = "インフォ表示"
MZ_LOG_PLUGIN = "MNKR_TMLogWindowMZ"
MZ_LOG_COMMAND = "addLog"
MZ_LOG_ARGUMENT_PATH = "text"

TEXT_LIKE_KEYS = frozenset(
    {
        "text", "message", "messagetext", "displaytext", "description",
        "help", "title", "label", "caption", "name",
    }
)
_INTERNAL_KEYS = frozenset(
    {
        "id", "key", "file", "filename", "path", "image", "picture",
        "audio", "bgm", "bgs", "me", "se", "plugin", "command",
        "script", "switch", "variable", "font", "align", "window",
    }
)
_JAPANESE = re.compile(r"[ぁ-ゖァ-ヺｦ-ﾟ㐀-䶿一-鿿豈-﫿]")
_ASCII_WORD = re.compile(r"[A-Za-z]{2,}")
_PATH_LIKE = re.compile(r"(?:[A-Za-z]:[\\/]|[/\\]|\.(?:png|jpe?g|ogg|m4a|wav|json|js)\b)", re.I)
_CONTROL_CODE = re.compile(r"\\(?:[VNCIP]\[\d+\]|G|[{}.$|!><^])", re.I)
_MV_PARTS = re.compile(
    r"^(?P<leading>\s*)(?P<prefix>\S+)(?P<separator>\s+)(?P<payload>[\s\S]*)$"
)
_ANNOTATION = re.compile(
    r"^(?P<head>\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*)(?P<value>[\s\S]*)$"
)


@dataclass(frozen=True, slots=True)
class MvPluginText:
    leading: str
    prefix: str
    separator: str
    payload: str
    classification: str
    rule_id: str | None = None

    def rebuild(self, translation: str) -> str:
        if self.classification != VERIFIED:
            raise ValueError("only verified MV plugin text may be rebuilt")
        return f"{self.leading}{self.prefix}{self.separator}{translation}"


@dataclass(frozen=True, slots=True)
class MzArgumentText:
    path: str
    value: str
    classification: str
    rule_id: str | None = None


def classify_mv_command(raw: str) -> MvPluginText:
    """Split one MV command without losing its exact prefix separator."""

    match = _MV_PARTS.match(raw)
    if match is None:
        return MvPluginText("", raw.strip(), "", "", INTERNAL)
    leading = match.group("leading")
    prefix = match.group("prefix")
    separator = match.group("separator")
    payload = match.group("payload")
    upper = prefix.upper()
    if prefix == MV_INFO_PREFIX and payload.strip():
        return MvPluginText(leading, prefix, separator, payload, VERIFIED, "mv_info_display")
    if upper in {"P_SHAKE", "P_SPIN_RELATIVE"}:
        return MvPluginText(leading, prefix, separator, payload, INTERNAL)
    if upper == "D_TEXT_SETTING":
        return MvPluginText(leading, prefix, separator, payload, INTERNAL)
    path_probe = _CONTROL_CODE.sub("", payload)
    looks_textual = bool(
        payload.strip()
        and not _PATH_LIKE.search(path_probe)
        and (_JAPANESE.search(payload) or _ASCII_WORD.search(payload))
    )
    classification = CONDITIONAL if looks_textual else INTERNAL
    return MvPluginText(leading, prefix, separator, payload, classification)


def iter_mz_argument_texts(
    plugin_name: str,
    command_name: str,
    arguments: Any,
    *,
    max_depth: int = 16,
) -> Iterator[MzArgumentText]:
    """Boundedly classify strings below an MZ command argument object."""

    def walk(value: Any, path: str, key_hint: str | None, depth: int) -> Iterator[MzArgumentText]:
        if depth > max_depth:
            return
        if isinstance(value, str):
            if not value.strip():
                return
            folded = key_hint.casefold() if key_hint else ""
            if (
                plugin_name == MZ_LOG_PLUGIN
                and command_name == MZ_LOG_COMMAND
                and path == MZ_LOG_ARGUMENT_PATH
            ):
                yield MzArgumentText(path, value, VERIFIED, "mz_tm_log_add_log_text")
            elif folded in _INTERNAL_KEYS:
                return
            elif folded in TEXT_LIKE_KEYS or _JAPANESE.search(value):
                yield MzArgumentText(path, value, CONDITIONAL)
            return
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    continue
                child_path = f"{path}.{key}" if path else key
                yield from walk(child, child_path, key, depth + 1)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                child_path = f"{path}[{index}]" if path else f"[{index}]"
                yield from walk(child, child_path, key_hint, depth + 1)

    yield from walk(arguments, "", None, 0)


def parse_editor_annotation(value: str) -> tuple[str, str, str] | None:
    """Return (argument key, payload, preserved prefix) for a code-657 row."""

    match = _ANNOTATION.match(value)
    if match is None:
        return None
    return match.group("key"), match.group("value"), match.group("head")


def rebuild_editor_annotation(value: str, translation: str) -> str:
    parsed = parse_editor_annotation(value)
    if parsed is None:
        raise ValueError("annotation is not a key=value row")
    return f"{parsed[2]}{translation}"


__all__ = [
    "CONDITIONAL", "INTERNAL", "MV_INFO_PREFIX", "MZ_LOG_ARGUMENT_PATH",
    "MZ_LOG_COMMAND", "MZ_LOG_PLUGIN", "MzArgumentText", "MvPluginText",
    "TEXT_LIKE_KEYS", "VERIFIED", "classify_mv_command",
    "iter_mz_argument_texts", "parse_editor_annotation",
    "rebuild_editor_annotation",
]
