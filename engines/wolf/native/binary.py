"""Bounded binary-reading helpers for future WOLF parser experiments."""

from __future__ import annotations

import io
import struct


class NativeFormatError(ValueError):
    """Raised when a bounded native-format invariant is violated."""


class BoundedBinaryReader:
    """Small fail-closed reader; it never allocates from an unchecked length."""

    def __init__(self, data: bytes, *, max_allocation: int = 1024 * 1024) -> None:
        if max_allocation < 1:
            raise ValueError("max_allocation must be positive")
        self._stream = io.BytesIO(data)
        self._size = len(data)
        self.max_allocation = max_allocation

    @property
    def position(self) -> int:
        return self._stream.tell()

    @property
    def remaining(self) -> int:
        return self._size - self.position

    def read_exact(self, size: int) -> bytes:
        if size < 0:
            raise NativeFormatError("negative read length")
        if size > self.max_allocation:
            raise NativeFormatError(
                f"read length {size} exceeds allocation limit {self.max_allocation}"
            )
        data = self._stream.read(size)
        if len(data) != size:
            raise NativeFormatError(
                f"truncated input at offset {self.position - len(data)}: "
                f"wanted {size} byte(s), found {len(data)}"
            )
        return data

    def read_u8(self) -> int:
        return self.read_exact(1)[0]

    def read_u32le(self) -> int:
        return struct.unpack("<I", self.read_exact(4))[0]

    def read_length_prefixed_bytes(self, *, require_nul: bool = False) -> bytes:
        length = self.read_u32le()
        payload = self.read_exact(length)
        if require_nul:
            if not payload or payload[-1] != 0:
                raise NativeFormatError("length-prefixed field has no NUL terminator")
            return payload[:-1]
        return payload

    def expect(self, expected: bytes) -> None:
        offset = self.position
        actual = self.read_exact(len(expected))
        if actual != expected:
            raise NativeFormatError(
                f"signature mismatch at offset {offset}: "
                f"expected {expected.hex()}, found {actual.hex()}"
            )


__all__ = ["BoundedBinaryReader", "NativeFormatError"]
