"""Follow latest.log across restarts.

The server writes a fresh latest.log every start and gzips the previous one.
A tail that holds a file descriptor follows the old inode into oblivion and
goes silent forever -- the failure is invisible, because nothing errors. So
this watches the *path* and re-opens whenever the inode or size says the file
underneath changed.
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable


class LogTail:
    def __init__(self, path: str, poll: float = 1.0, from_start: bool = False,
                 max_line: int = 8192):
        self.path = path
        self.poll = poll
        self.from_start = from_start
        self.max_line = max_line
        self._fh = None
        self._inode: tuple[int, int] | None = None

    def _stat_key(self) -> tuple[int, int] | None:
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return None
        return (st.st_dev, st.st_ino)

    def _open(self, seek_end: bool) -> bool:
        try:
            fh = open(self.path, "r", encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return False
        if seek_end:
            fh.seek(0, os.SEEK_END)
        if self._fh:
            self._fh.close()
        self._fh = fh
        self._inode = self._stat_key()
        return True

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

    async def lines(self) -> AsyncIterator[str]:
        """Yield lines forever, surviving rotation and the file going missing."""
        self._open(seek_end=not self.from_start)
        while True:
            if self._fh is None:
                if not self._open(seek_end=False):
                    await asyncio.sleep(self.poll)
                    continue

            line = self._fh.readline()
            if line:
                if line.endswith("\n"):
                    yield line.rstrip("\n")[: self.max_line]
                    continue
                # Partial write: rewind and wait for the writer to finish.
                self._fh.seek(self._fh.tell() - len(line))
                await asyncio.sleep(self.poll)
                continue

            await asyncio.sleep(self.poll)

            key = self._stat_key()
            if key is None:
                # File vanished mid-restart; wait for the new one to appear.
                self.close()
                continue
            if key != self._inode:
                # Rotated: a new latest.log exists. Read it from the top so
                # nothing written during the swap is lost.
                self._open(seek_end=False)
                continue
            try:
                if os.path.getsize(self.path) < self._fh.tell():
                    # Truncated in place rather than replaced.
                    self._fh.seek(0)
            except OSError:
                self.close()


async def pump(tail: LogTail, handle: Callable[[str], None]) -> None:
    async for line in tail.lines():
        handle(line)
