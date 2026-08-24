"""Bounded, read-only probes for WOLF archive candidates.

No binary signature, entry table, decryption key, or archive generation is
inferred here.  The official documentation does not publish enough of that
format contract for a safe parser, so this module reports observations and
clearly labelled filename/environment heuristics only.
"""

from __future__ import annotations

import math
from pathlib import Path

from core.archive import ArchiveFormatInfo, ArchiveReport
from core.paths import portable_relative_path


OFFICIAL_CONTROL_URL = "https://silversecond.com/WolfRPGEditor/Help/01control.html"
OFFICIAL_PRO_URL = "https://silversecond.com/WolfRPGEditor/Help/06pro_version.html"
OFFICIAL_WOLFX_URL = (
    "https://silversecond.com/WolfRPGEditor/Help/02_file_crypt_pro.html"
)

PROBE_WINDOW_BYTES = 4096
PROBE_WINDOW_COUNT = 4
MAX_ARCHIVE_SAMPLE_BYTES = PROBE_WINDOW_BYTES * PROBE_WINDOW_COUNT
HEADER_BYTES = 32
TAIL_BYTES = 32
MAX_COMPANION_ENTRIES = 256


WOLF_ARCHIVE_FORMATS: dict[str, ArchiveFormatInfo] = {
    ".wolf": ArchiveFormatInfo(
        format="wolf_archive",
        extensions=(".wolf",),
        header=None,
        generation="unknown",
        encryption="official output can be encrypted; observed file not verified",
        compression="unknown",
        confidence=70,
        verified=(
            "Editor game-data creation can encrypt Data into Data.wolf",
            "folder-level output can create multiple .wolf files",
            "official commands expose selectable encryption generations",
        ),
        probable=(
            "a .wolf beside a WOLF executable is a WOLF archive candidate",
        ),
        unknown=(
            "binary header signature",
            "archive generation from bounded bytes",
            "compression and key details for this file",
            "entry table layout",
        ),
        sources=(OFFICIAL_CONTROL_URL,),
        notes=("Extension and surroundings are evidence, not content proof.",),
    ),
    ".wolfx": ArchiveFormatInfo(
        format="wolfx_individual_encrypted_file",
        extensions=(".wolfx",),
        header=None,
        generation="unknown",
        encryption="individual Pro encryption; key use is project-defined",
        compression="unknown",
        confidence=75,
        verified=(
            ".wolfx is Pro individual-file encryption",
            "the output name appends .wolfx to the original filename",
            "font and map files can be individually encrypted",
            "runtime string/numeric/decryption-key conditions may be used",
        ),
        probable=(),
        unknown=(
            "binary header signature",
            "encryption generation from bounded bytes",
            "keys required by this particular file",
            "compression and payload layout",
        ),
        sources=(OFFICIAL_WOLFX_URL,),
        notes=(".wolfx is not treated as the same container as .wolf.",),
    ),
    ".assets": ArchiveFormatInfo(
        format="wolf_custom_extension_candidate",
        extensions=(".assets",),
        header=None,
        generation="unknown",
        encryption="unknown",
        compression="unknown",
        confidence=30,
        verified=(
            "WOLF Pro can change the normal .wolf extension to a custom extension such as .assets",
        ),
        probable=(
            "an .assets file beside WOLF evidence may be a renamed WOLF archive",
        ),
        unknown=(
            "whether this particular file is a WOLF archive",
            "binary header, generation, encryption, compression, and entries",
        ),
        sources=(OFFICIAL_PRO_URL,),
    ),
}

UNKNOWN_ARCHIVE_FORMAT = ArchiveFormatInfo(
    format="unknown",
    extensions=(),
    header=None,
    generation="unknown",
    encryption="unknown",
    compression="unknown",
    confidence=0,
    unknown=("format, header, generation, encryption, compression, and entries",),
)


class WolfArchiveProbe:
    """Observe a candidate with seek-based bounded reads and no writes."""

    def probe(self, archive_file: Path) -> ArchiveReport:
        if not archive_file.is_file():
            raise FileNotFoundError(f"archive file does not exist: {archive_file}")
        if archive_file.is_symlink():
            raise ValueError("symbolic-link archive inputs are not inspected")

        size = archive_file.stat().st_size
        extension = archive_file.suffix.casefold()
        format_info = WOLF_ARCHIVE_FORMATS.get(
            extension, UNKNOWN_ARCHIVE_FORMAT
        )
        root = find_wolf_game_root(archive_file)
        relative_path = (
            portable_relative_path(archive_file, root)
            if root is not None
            else archive_file.name
        )
        sampled, header, tail, bytes_read = _sample_file(archive_file, size)
        executable_type, executable_file, pe_header_present = _executable_metadata(
            root
        )
        companions = _companion_files(archive_file, root)
        role, role_confidence, text_likelihood = classify_wolf_archive_role(
            archive_file.name
        )

        evidence: list[str] = []
        if extension in WOLF_ARCHIVE_FORMATS:
            evidence.append(f"recognized extension: {extension}")
        else:
            evidence.append(f"unrecognized extension: {extension or '<none>'}")
        if executable_file is not None:
            evidence.append(f"companion executable: {executable_file}")
        if role != "unknown_archive_role":
            evidence.append(f"filename heuristic role: {role}")

        confidence = format_info.confidence
        if executable_type != "unknown":
            confidence = min(90, confidence + 10)
        if role_confidence == "probable":
            confidence = min(90, confidence + 5)

        if extension == ".wolf":
            archive_type = "wolf_archive"
            packaging = "archive_candidate"
            encryption_status = "probably_encrypted"
        elif extension == ".wolfx":
            archive_type = "wolfx_individual_encrypted_file"
            packaging = "individual_encrypted_file"
            encryption_status = "probably_encrypted"
        elif extension == ".assets":
            archive_type = "wolf_custom_extension_candidate"
            packaging = "custom_extension_candidate"
            encryption_status = "unknown"
        else:
            archive_type = "unknown"
            packaging = "unknown"
            encryption_status = "unknown"

        notes = [
            "Read-only bounded probe; archive contents were not parsed or decrypted.",
            "Header bytes are observations only and are not treated as a verified signature.",
            "Entry listing is disabled because no sufficiently verified format contract is integrated.",
        ]
        if extension == ".wolfx":
            notes.append(".wolfx was probed separately from the .wolf archive family.")
        if root is None:
            notes.append("No nearby WOLF game root was identified.")

        return ArchiveReport(
            path=archive_file.name,
            relative_path=relative_path,
            size=size,
            extension=extension,
            archive_type=archive_type,
            header_hex=header.hex(),
            tail_hex=tail.hex(),
            packaging=packaging,
            encryption_status=encryption_status,
            version=None,
            version_confidence="unknown",
            confidence=confidence,
            evidence=tuple(evidence),
            executable_type=executable_type,
            executable_file=executable_file,
            companion_files=companions,
            entry_listing_supported=False,
            entry_count=None,
            entries=(),
            notes=tuple(notes),
            extra_metadata={
                "archive_role": role,
                "role_confidence": role_confidence,
                "role_basis": "filename_heuristic",
                "text_likelihood": text_likelihood,
                "sample_metrics": _sample_metrics(sampled, bytes_read),
                "probe_limits": {
                    "window_bytes": PROBE_WINDOW_BYTES,
                    "window_count": PROBE_WINDOW_COUNT,
                    "maximum_archive_bytes_read": MAX_ARCHIVE_SAMPLE_BYTES,
                },
                "executable_metadata": {
                    "pe_header_present": pe_header_present,
                    "version_marker": None,
                    "version_marker_status": "unknown",
                },
                "format_knowledge": format_info.to_json_dict(),
            },
        )


def find_wolf_game_root(archive_file: Path) -> Path | None:
    """Find a nearby root from portable layout evidence, not a stored path."""

    current = archive_file.parent
    for _ in range(5):
        children = _bounded_children(current)
        names = {child.name.casefold(): child for child in children}
        has_known_executable = any(
            name in names and _safe_is_file(names[name])
            for name in ("game.exe", "gamepro.exe")
        )
        has_renamed_candidate = any(
            _safe_is_file(child)
            and child.suffix.casefold() == ".exe"
            and child.name.casefold()
            not in {"config.exe", "editor.exe", "editorpro.exe"}
            for child in children
        )
        data = names.get("data")
        archive_under_data = False
        if data is not None and _safe_is_dir(data):
            try:
                archive_file.relative_to(data)
                archive_under_data = True
            except ValueError:
                pass
        if has_known_executable or (has_renamed_candidate and archive_under_data):
            return current
        if current.parent == current:
            break
        current = current.parent
    return None


def classify_wolf_archive_role(filename: str) -> tuple[str, str, str]:
    """Return role, confidence, and text likelihood from filename only."""

    name = filename.casefold()
    original_name = name[:-6] if name.endswith(".wolfx") else name
    stem = Path(original_name).stem.casefold()
    if name.endswith(".wolfx") and Path(original_name).suffix in {
        ".ttf",
        ".ttc",
        ".otf",
        ".woff",
        ".woff2",
    }:
        return "possible_font_asset", "probable", "low"
    exact = {
        "basicdata": ("possible_basic_data", "high"),
        "mapdata": ("possible_map_data", "high"),
        "text_script": ("possible_script_or_text_data", "high"),
        "script": ("possible_script_or_text_data", "high"),
        "mdb": ("possible_database_or_text_data", "high"),
        "tdb": ("possible_database_or_text_data", "high"),
        "game": ("possible_game_data", "medium"),
        "systemfile": ("possible_system_data", "medium"),
        "data": ("possible_complete_data", "medium"),
    }
    if stem in exact:
        role, likelihood = exact[stem]
        return role, "probable", likelihood
    if "text" in stem or "script" in stem:
        return "possible_script_or_text_data", "probable", "high"
    return "unknown_archive_role", "unknown", "unknown"


def _sample_file(
    path: Path, size: int
) -> tuple[bytes, bytes, bytes, int]:
    if size <= 0:
        return b"", b"", b"", 0
    maximum_start = max(0, size - PROBE_WINDOW_BYTES)
    positions = {
        0,
        min(maximum_start, size // 4),
        min(maximum_start, size // 2),
        maximum_start,
    }
    chunks: dict[int, bytes] = {}
    with path.open("rb") as stream:
        for position in sorted(positions):
            stream.seek(position)
            chunks[position] = stream.read(PROBE_WINDOW_BYTES)
    first = chunks[min(chunks)]
    last = chunks[max(chunks)]
    sampled = b"".join(chunks[position] for position in sorted(chunks))
    return sampled, first[:HEADER_BYTES], last[-TAIL_BYTES:], len(sampled)


def _sample_metrics(sampled: bytes, bytes_read: int) -> dict[str, object]:
    if not sampled:
        return {
            "bytes_sampled": bytes_read,
            "shannon_entropy_bits_per_byte": 0.0,
            "zero_byte_percent": 0.0,
            "printable_ascii_percent": 0.0,
        }
    counts = [0] * 256
    for value in sampled:
        counts[value] += 1
    length = len(sampled)
    entropy = -sum(
        (count / length) * math.log2(count / length)
        for count in counts
        if count
    )
    printable = sum(value in {9, 10, 13} or 32 <= value <= 126 for value in sampled)
    return {
        "bytes_sampled": bytes_read,
        "shannon_entropy_bits_per_byte": round(entropy, 4),
        "zero_byte_percent": round(counts[0] * 100 / length, 4),
        "printable_ascii_percent": round(printable * 100 / length, 4),
    }


def _executable_metadata(
    root: Path | None,
) -> tuple[str, str | None, bool | None]:
    if root is None:
        return "unknown", None, None
    children = _bounded_children(root)
    by_name = {
        child.name.casefold(): child for child in children if _safe_is_file(child)
    }
    executable: Path | None = None
    executable_type = "unknown"
    if "gamepro.exe" in by_name:
        executable = by_name["gamepro.exe"]
        executable_type = "game_pro"
    elif "game.exe" in by_name:
        executable = by_name["game.exe"]
        executable_type = "game"
    else:
        candidates = sorted(
            (
                child
                for child in children
                if _safe_is_file(child)
                and child.suffix.casefold() == ".exe"
                and child.name.casefold()
                not in {"config.exe", "editor.exe", "editorpro.exe"}
            ),
            key=lambda item: item.name.casefold(),
        )
        if candidates:
            executable = candidates[0]
            executable_type = "renamed_candidate"
    if executable is None:
        return executable_type, None, None
    try:
        with executable.open("rb") as stream:
            pe_header_present = stream.read(2) == b"MZ"
    except OSError:
        pe_header_present = None
    return executable_type, executable.name, pe_header_present


def _companion_files(archive_file: Path, root: Path | None) -> tuple[str, ...]:
    candidates: set[Path] = set()
    for child in _bounded_children(archive_file.parent):
        if _safe_is_file(child) and (
            child.suffix.casefold() in {".wolf", ".wolfx", ".assets"}
            or child.name.casefold()
            in {"game.exe", "gamepro.exe", "config.exe", "editor.exe", "editorpro.exe"}
        ):
            candidates.add(child)
    if root is not None:
        for child in _bounded_children(root):
            if _safe_is_file(child) and child.suffix.casefold() == ".exe":
                candidates.add(child)
    candidates.discard(archive_file)
    base = root or archive_file.parent
    return tuple(
        portable_relative_path(path, base)
        for path in sorted(candidates, key=lambda item: item.as_posix().casefold())
    )


def _bounded_children(directory: Path) -> list[Path]:
    try:
        children: list[Path] = []
        for index, child in enumerate(directory.iterdir()):
            if index >= MAX_COMPANION_ENTRIES:
                break
            if not child.is_symlink():
                children.append(child)
        return children
    except OSError:
        return []


def _safe_is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _safe_is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


__all__ = [
    "MAX_ARCHIVE_SAMPLE_BYTES",
    "WOLF_ARCHIVE_FORMATS",
    "WolfArchiveProbe",
    "classify_wolf_archive_role",
    "find_wolf_game_root",
]
