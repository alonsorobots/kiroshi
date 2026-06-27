"""Atomic writes — never leave a partial file on crash / power-loss.

Pattern: write to a temp file in the *same directory* (so os.replace is atomic on
the same filesystem), fsync, then atomically replace the target. A Runner that
dies mid-write leaves only a stray .tmp, never a half-written output that would
fool the output-exists resume check.
"""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Union

PathLike = Union[str, "os.PathLike[str]"]


def ensure_parent(path: PathLike) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def atomic_write_bytes(path: PathLike, data: bytes, fsync: bool = True) -> None:
    path = Path(path)
    ensure_parent(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            if fsync:
                os.fsync(f.fileno())
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def atomic_write_text(path: PathLike, text: str, encoding: str = "utf-8", fsync: bool = True) -> None:
    atomic_write_bytes(path, text.encode(encoding), fsync=fsync)


@contextmanager
def atomic_path(path: PathLike, fsync: bool = True) -> Iterator[Path]:
    """Yield a temp Path to write into; atomically promote to `path` on success.

    Usage::

        with atomic_path("out/clip.dat") as tmp:
            np.save(tmp, arr)   # or any writer that takes a path
    """
    path = Path(path)
    ensure_parent(path)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        yield tmp
        if fsync:
            try:
                _fsync_file(tmp)
            except OSError:
                pass
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None and tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _fsync_file(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
