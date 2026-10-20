"""Streaming log writer for experiment output (DEVELOPMENT.md §11).

The child's output is written to disk **while it runs**, so a multi-hour training
never keeps its log in memory. The encoding is sniffed from the first bytes
(UTF-8 first, GBK fallback — the LabBox lesson) and the decoded text is stored as
UTF-8, so the log is readable no matter what the experiment printed.
"""

from __future__ import annotations

import codecs
from pathlib import Path
from typing import BinaryIO

#: Bytes buffered before the encoding is decided.
SNIFF_BYTES = 32 * 1024
#: Read size when pumping the child's output.
CHUNK_BYTES = 64 * 1024


class LogStream:
    """Write child output to a UTF-8 log file without buffering it in memory."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: BinaryIO = path.open("wb")
        self._decoder: codecs.IncrementalDecoder | None = None
        self._pending = b""

    @property
    def path(self) -> Path:
        return self._path

    def write(self, chunk: bytes) -> None:
        if self._decoder is not None:
            self._emit(chunk)
            return
        self._pending += chunk
        if len(self._pending) < SNIFF_BYTES:
            return
        pending, self._pending = self._pending, b""
        self._decoder = self._sniff(pending)
        self._emit(pending)

    def close(self) -> None:
        try:
            if self._decoder is None:
                pending, self._pending = self._pending, b""
                self._decoder = self._sniff(pending)
                self._emit(pending)
            else:
                self._emit(b"")
        finally:
            self._handle.close()

    def append_text(self, text: str) -> None:
        """Append a UTF-8 line to the finished log (e.g. the interrupt marker)."""
        with self._path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(text)

    def _emit(self, data: bytes) -> None:
        if not data or self._decoder is None:
            return
        text = self._decoder.decode(data)
        if text:
            self._handle.write(text.encode("utf-8"))

    @staticmethod
    def _sniff(sample: bytes) -> codecs.IncrementalDecoder:
        try:
            codecs.getincrementaldecoder("utf-8")(errors="strict").decode(sample, final=False)
        except UnicodeDecodeError:
            return codecs.getincrementaldecoder("gbk")(errors="replace")
        return codecs.getincrementaldecoder("utf-8")(errors="replace")
