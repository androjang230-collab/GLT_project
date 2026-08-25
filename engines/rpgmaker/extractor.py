"""Safe, schema-driven text extraction for RPG Maker MV/MZ JSON data."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from core.models import (
    EngineId,
    ExtractionIssue,
    ExtractionResult,
    TranslationEntry,
)
from core.translation_io import write_jsonl


_MAP_FILE_PATTERN = re.compile(r"Map(\d+)\.json", re.IGNORECASE)
_CONTROL_CODE_PATTERN = re.compile(
    r"\\(?:[VNCIP]\[\d+\]|G|[{}.$|!><^])",
    re.IGNORECASE,
)

_ORDERED_DATA_FILES = (
    "CommonEvents.json",
    "Actors.json",
    "Classes.json",
    "Items.json",
    "Weapons.json",
    "Armors.json",
    "Skills.json",
    "States.json",
    "Enemies.json",
    "Troops.json",
    "System.json",
)

_DATABASE_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "Actors.json": (
        ("name", "actor_name"),
        ("nickname", "actor_nickname"),
        ("profile", "description"),
    ),
    "Classes.json": (("name", "class_name"),),
    "Items.json": (
        ("name", "item_name"),
        ("description", "description"),
    ),
    "Weapons.json": (
        ("name", "weapon_name"),
        ("description", "description"),
    ),
    "Armors.json": (
        ("name", "armor_name"),
        ("description", "description"),
    ),
    "Skills.json": (
        ("name", "skill_name"),
        ("description", "description"),
        ("message1", "system"),
        ("message2", "system"),
    ),
    "States.json": (
        ("name", "state_name"),
        ("message1", "system"),
        ("message2", "system"),
        ("message3", "system"),
        ("message4", "system"),
    ),
    "Enemies.json": (("name", "enemy_name"),),
}

_SYSTEM_SCALARS = (
    ("gameTitle", "system"),
    ("currencyUnit", "ui"),
)
_SYSTEM_ARRAYS = (
    "elements",
    "skillTypes",
    "weaponTypes",
    "armorTypes",
    "equipTypes",
)


class SourceFormatError(ValueError):
    """Raised when valid JSON has an unsupported top-level structure."""


def find_control_codes(text: str) -> tuple[str, ...]:
    """Return RPG Maker control codes in source order, including repetitions."""

    return tuple(match.group(0) for match in _CONTROL_CODE_PATTERN.finditer(text))


class RpgMakerExtractor:
    """Extract only explicitly approved, player-visible RPG Maker fields."""

    def __init__(self, engine: EngineId) -> None:
        if engine not in (EngineId.RPGMAKER_MV, EngineId.RPGMAKER_MZ):
            raise ValueError(f"unsupported engine: {engine}")
        self.engine = engine

    def extract(self, game_directory: Path) -> ExtractionResult:
        data_directory = game_directory / "data"
        result = ExtractionResult()
        if not data_directory.is_dir():
            result.issues.append(
                ExtractionIssue("data", "data directory does not exist")
            )
            return result

        map_names = self._load_map_names(data_directory, result)

        seen_ids: dict[str, str] = {}
        for source_file in self._source_files(data_directory):
            relative_file = f"data/{source_file.name}"
            try:
                document = self._load_json(source_file)
                entries = self._extract_document(
                    source_file.name,
                    document,
                    map_names,
                )
            except json.JSONDecodeError as exc:
                result.issues.append(
                    ExtractionIssue(
                        relative_file,
                        (
                            f"invalid JSON at line {exc.lineno}, column {exc.colno} "
                            f"(character {exc.pos}): {exc.msg}"
                        ),
                    )
                )
                continue
            except UnicodeDecodeError as exc:
                result.issues.append(
                    ExtractionIssue(
                        relative_file,
                        f"invalid UTF-8 at byte {exc.start}: {exc.reason}",
                    )
                )
                continue
            except (OSError, SourceFormatError) as exc:
                result.issues.append(ExtractionIssue(relative_file, str(exc)))
                continue

            for entry in entries:
                first_file = seen_ids.get(entry.id)
                if first_file is not None:
                    result.issues.append(
                        ExtractionIssue(
                            relative_file,
                            f"duplicate translation ID {entry.id!r}; first seen in {first_file}",
                        )
                    )
                    continue
                seen_ids[entry.id] = relative_file
                result.entries.append(entry)
        return result

    @classmethod
    def source_files(cls, game_directory: Path) -> list[Path]:
        """Return deterministic Phase 2 source files for manifests/fingerprints."""

        data_directory = game_directory / "data"
        if not data_directory.is_dir():
            return []
        return cls._source_files(data_directory)

    @staticmethod
    def _source_files(data_directory: Path) -> list[Path]:
        map_files: list[tuple[int, str, Path]] = []
        for path in data_directory.iterdir():
            if not path.is_file():
                continue
            match = _MAP_FILE_PATTERN.fullmatch(path.name)
            if match:
                map_files.append((int(match.group(1)), path.name.casefold(), path))
        map_files.sort(key=lambda item: (item[0], item[1]))

        files = [item[2] for item in map_files]
        for file_name in _ORDERED_DATA_FILES:
            path = data_directory / file_name
            if path.is_file():
                files.append(path)
        return files

    @staticmethod
    def _load_json(source_file: Path) -> Any:
        with source_file.open("r", encoding="utf-8-sig") as stream:
            return json.load(stream)

    def _extract_document(
        self,
        file_name: str,
        document: Any,
        map_names: dict[int, str],
    ) -> list[TranslationEntry]:
        if _MAP_FILE_PATTERN.fullmatch(file_name):
            return self._extract_map(file_name, document, map_names)
        if file_name == "CommonEvents.json":
            return self._extract_common_events(file_name, document)
        if file_name == "Troops.json":
            return self._extract_troops(file_name, document)
        if file_name == "System.json":
            return self._extract_system(file_name, document)
        if file_name in _DATABASE_FIELDS:
            return self._extract_database(file_name, document)
        return []

    def _extract_map(
        self,
        file_name: str,
        document: Any,
        map_names: dict[int, str],
    ) -> list[TranslationEntry]:
        if not isinstance(document, dict):
            raise SourceFormatError("map JSON root must be an object")

        entries: list[TranslationEntry] = []
        file_stem = Path(file_name).stem
        display_name = document.get("displayName")
        if _is_text(display_name):
            entries.append(
                self._entry(
                    entry_id=f"{file_stem}:displayName",
                    file_name=file_name,
                    text=display_name,
                    text_type="ui",
                    json_path="$.displayName",
                )
            )

        events = document.get("events", [])
        if not isinstance(events, list):
            raise SourceFormatError("map events field must be an array")
        for event_array_index, event in enumerate(events):
            if not isinstance(event, dict):
                continue
            raw_event_id = event.get("id")
            event_id = raw_event_id if isinstance(raw_event_id, int) else event_array_index
            pages = event.get("pages", [])
            if not isinstance(pages, list):
                continue
            for page_index, page in enumerate(pages):
                if not isinstance(page, dict) or not isinstance(page.get("list"), list):
                    continue
                page_id = page_index + 1
                entries.extend(
                    self._extract_command_list(
                        file_name=file_name,
                        context_id=f"{file_stem}:event{event_id}:page{page_id}",
                        commands=page["list"],
                        json_prefix=(
                            f"$.events[{event_array_index}].pages[{page_index}].list"
                        ),
                        event_id=event_id,
                        page_id=page_id,
                    )
                )
        match = _MAP_FILE_PATTERN.fullmatch(file_name)
        map_id = int(match.group(1)) if match else None
        map_name = map_names.get(map_id) if map_id is not None else None
        return [
            replace(entry, map_id=map_id, map_name=map_name)
            for entry in entries
        ]

    @staticmethod
    def _load_map_names(
        data_directory: Path,
        result: ExtractionResult,
    ) -> dict[int, str]:
        map_infos_file = data_directory / "MapInfos.json"
        if not map_infos_file.is_file():
            return {}
        try:
            with map_infos_file.open("r", encoding="utf-8-sig") as stream:
                document = json.load(stream)
        except json.JSONDecodeError as exc:
            result.issues.append(
                ExtractionIssue(
                    "data/MapInfos.json",
                    (
                        f"invalid JSON at line {exc.lineno}, column {exc.colno} "
                        f"(character {exc.pos}): {exc.msg}"
                    ),
                )
            )
            return {}
        except (OSError, UnicodeDecodeError) as exc:
            result.issues.append(ExtractionIssue("data/MapInfos.json", str(exc)))
            return {}
        if not isinstance(document, list):
            result.issues.append(
                ExtractionIssue(
                    "data/MapInfos.json",
                    "MapInfos JSON root must be an array",
                )
            )
            return {}
        names: dict[int, str] = {}
        for index, record in enumerate(document):
            if not isinstance(record, dict):
                continue
            raw_id = record.get("id")
            map_id = raw_id if isinstance(raw_id, int) else index
            name = record.get("name")
            if isinstance(name, str):
                names[map_id] = name
        return names

    def _extract_common_events(
        self,
        file_name: str,
        document: Any,
    ) -> list[TranslationEntry]:
        if not isinstance(document, list):
            raise SourceFormatError("CommonEvents JSON root must be an array")
        entries: list[TranslationEntry] = []
        for array_index, common_event in enumerate(document):
            if not isinstance(common_event, dict):
                continue
            commands = common_event.get("list")
            if not isinstance(commands, list):
                continue
            raw_event_id = common_event.get("id")
            event_id = raw_event_id if isinstance(raw_event_id, int) else array_index
            entries.extend(
                self._extract_command_list(
                    file_name=file_name,
                    context_id=f"CommonEvents:commonEvent{event_id}",
                    commands=commands,
                    json_prefix=f"$[{array_index}].list",
                    event_id=event_id,
                    page_id=None,
                )
            )
        return entries

    def _extract_troops(
        self,
        file_name: str,
        document: Any,
    ) -> list[TranslationEntry]:
        if not isinstance(document, list):
            raise SourceFormatError("Troops JSON root must be an array")
        entries: list[TranslationEntry] = []
        for array_index, troop in enumerate(document):
            if not isinstance(troop, dict):
                continue
            raw_troop_id = troop.get("id")
            troop_id = raw_troop_id if isinstance(raw_troop_id, int) else array_index
            pages = troop.get("pages", [])
            if not isinstance(pages, list):
                continue
            for page_index, page in enumerate(pages):
                if not isinstance(page, dict) or not isinstance(page.get("list"), list):
                    continue
                page_id = page_index + 1
                entries.extend(
                    self._extract_command_list(
                        file_name=file_name,
                        context_id=f"Troops:troop{troop_id}:page{page_id}",
                        commands=page["list"],
                        json_prefix=f"$[{array_index}].pages[{page_index}].list",
                        event_id=troop_id,
                        page_id=page_id,
                    )
                )
        return entries

    def _extract_command_list(
        self,
        *,
        file_name: str,
        context_id: str,
        commands: list[Any],
        json_prefix: str,
        event_id: int,
        page_id: int | None,
    ) -> list[TranslationEntry]:
        entries: list[TranslationEntry] = []
        active_speaker: str | None = None
        for command_index, command in enumerate(commands):
            if not isinstance(command, dict):
                active_speaker = None
                continue
            code = command.get("code")
            parameters = command.get("parameters")
            if not isinstance(parameters, list):
                active_speaker = None
                continue

            if code == 101:
                active_speaker = None
                if len(parameters) > 4 and _is_text(parameters[4]):
                    active_speaker = parameters[4]
                    entries.append(
                        self._command_entry(
                            file_name=file_name,
                            context_id=context_id,
                            code=101,
                            command_index=command_index,
                            parameter_index=4,
                            text=parameters[4],
                            text_type="speaker",
                            json_path=f"{json_prefix}[{command_index}].parameters[4]",
                            event_id=event_id,
                            page_id=page_id,
                        )
                    )
                continue

            if code == 401:
                if parameters and _is_text(parameters[0]):
                    entries.append(
                        self._command_entry(
                            file_name=file_name,
                            context_id=context_id,
                            code=401,
                            command_index=command_index,
                            parameter_index=0,
                            text=parameters[0],
                            text_type="dialogue",
                            json_path=f"{json_prefix}[{command_index}].parameters[0]",
                            event_id=event_id,
                            page_id=page_id,
                            speaker=active_speaker,
                        )
                    )
                continue

            active_speaker = None
            if code == 102 and parameters and isinstance(parameters[0], list):
                for choice_index, choice in enumerate(parameters[0]):
                    if not _is_text(choice):
                        continue
                    entry_id = (
                        f"{context_id}:cmd102:index{command_index}:param0:"
                        f"choice{choice_index}"
                    )
                    entries.append(
                        self._entry(
                            entry_id=entry_id,
                            file_name=file_name,
                            text=choice,
                            text_type="choice",
                            json_path=(
                                f"{json_prefix}[{command_index}].parameters[0]"
                                f"[{choice_index}]"
                            ),
                            event_id=event_id,
                            page_id=page_id,
                            command_index=command_index,
                            parameter_index=0,
                        )
                    )
            elif code == 405 and parameters and _is_text(parameters[0]):
                entries.append(
                    self._command_entry(
                        file_name=file_name,
                        context_id=context_id,
                        code=405,
                        command_index=command_index,
                        parameter_index=0,
                        text=parameters[0],
                        text_type="scroll_text",
                        json_path=f"{json_prefix}[{command_index}].parameters[0]",
                        event_id=event_id,
                        page_id=page_id,
                    )
                )
            elif (
                code in (320, 324, 325)
                and len(parameters) > 1
                and _is_text(parameters[1])
            ):
                text_type = {
                    320: "actor_name",
                    324: "actor_nickname",
                    325: "description",
                }[code]
                entries.append(
                    self._command_entry(
                        file_name=file_name,
                        context_id=context_id,
                        code=code,
                        command_index=command_index,
                        parameter_index=1,
                        text=parameters[1],
                        text_type=text_type,
                        json_path=(
                            f"{json_prefix}[{command_index}].parameters[1]"
                        ),
                        event_id=event_id,
                        page_id=page_id,
                    )
                )
        return entries

    def _command_entry(
        self,
        *,
        file_name: str,
        context_id: str,
        code: int,
        command_index: int,
        parameter_index: int,
        text: str,
        text_type: str,
        json_path: str,
        event_id: int,
        page_id: int | None,
        speaker: str | None = None,
    ) -> TranslationEntry:
        return self._entry(
            entry_id=(
                f"{context_id}:cmd{code}:index{command_index}:param{parameter_index}"
            ),
            file_name=file_name,
            text=text,
            text_type=text_type,
            speaker=speaker,
            json_path=json_path,
            event_id=event_id,
            page_id=page_id,
            command_index=command_index,
            parameter_index=parameter_index,
        )

    def _extract_database(
        self,
        file_name: str,
        document: Any,
    ) -> list[TranslationEntry]:
        if not isinstance(document, list):
            raise SourceFormatError(f"{file_name} JSON root must be an array")
        entries: list[TranslationEntry] = []
        file_stem = Path(file_name).stem
        for array_index, record in enumerate(document):
            if not isinstance(record, dict):
                continue
            for field_name, text_type in _DATABASE_FIELDS[file_name]:
                value = record.get(field_name)
                if not _is_text(value):
                    continue
                entries.append(
                    self._entry(
                        entry_id=f"{file_stem}:index{array_index}:{field_name}",
                        file_name=file_name,
                        text=value,
                        text_type=text_type,
                        json_path=f"$[{array_index}].{field_name}",
                    )
                )
        return entries

    def _extract_system(self, file_name: str, document: Any) -> list[TranslationEntry]:
        if not isinstance(document, dict):
            raise SourceFormatError("System JSON root must be an object")
        entries: list[TranslationEntry] = []
        for field_name, text_type in _SYSTEM_SCALARS:
            value = document.get(field_name)
            if _is_text(value):
                entries.append(
                    self._entry(
                        entry_id=f"System:{field_name}",
                        file_name=file_name,
                        text=value,
                        text_type=text_type,
                        json_path=f"$.{field_name}",
                    )
                )

        for field_name in _SYSTEM_ARRAYS:
            values = document.get(field_name)
            if not isinstance(values, list):
                continue
            for array_index, value in enumerate(values):
                if _is_text(value):
                    entries.append(
                        self._entry(
                            entry_id=f"System:{field_name}:index{array_index}",
                            file_name=file_name,
                            text=value,
                            text_type="ui",
                            json_path=f"$.{field_name}[{array_index}]",
                        )
                    )

        terms = document.get("terms")
        if isinstance(terms, (dict, list)):
            entries.extend(
                self._extract_system_terms(
                    file_name=file_name,
                    value=terms,
                    id_tokens=("terms",),
                    json_path="$.terms",
                )
            )
        return entries

    def _extract_system_terms(
        self,
        *,
        file_name: str,
        value: Any,
        id_tokens: tuple[str, ...],
        json_path: str,
    ) -> list[TranslationEntry]:
        if _is_text(value):
            text_type = "system" if "messages" in id_tokens else "ui"
            return [
                self._entry(
                    entry_id="System:" + ":".join(_id_token(token) for token in id_tokens),
                    file_name=file_name,
                    text=value,
                    text_type=text_type,
                    json_path=json_path,
                )
            ]

        entries: list[TranslationEntry] = []
        if isinstance(value, list):
            for array_index, child in enumerate(value):
                entries.extend(
                    self._extract_system_terms(
                        file_name=file_name,
                        value=child,
                        id_tokens=(*id_tokens, f"index{array_index}"),
                        json_path=f"{json_path}[{array_index}]",
                    )
                )
        elif isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    continue
                entries.extend(
                    self._extract_system_terms(
                        file_name=file_name,
                        value=child,
                        id_tokens=(*id_tokens, key),
                        json_path=f"{json_path}.{key}",
                    )
                )
        return entries

    def _entry(
        self,
        *,
        entry_id: str,
        file_name: str,
        text: str,
        text_type: str,
        speaker: str | None = None,
        json_path: str | None = None,
        event_id: int | None = None,
        page_id: int | None = None,
        command_index: int | None = None,
        parameter_index: int | None = None,
    ) -> TranslationEntry:
        return TranslationEntry(
            id=entry_id,
            engine=self.engine,
            file=f"data/{file_name}",
            type=text_type,
            original=text,
            speaker=speaker,
            json_path=json_path,
            event_id=event_id,
            page_id=page_id,
            command_index=command_index,
            parameter_index=parameter_index,
            control_codes=find_control_codes(text),
        )


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _id_token(value: str) -> str:
    return value.replace("~", "~0").replace(":", "~1")
