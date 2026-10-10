"""Board files a script reads through the filesystem, served without a Pi.

Some of what a script learns about the board isn't a library call but a file:
gpiozero and RPi.GPIO read the revision from /proc/device-tree, 1-Wire sensors
are read from /sys/bus/w1/devices, and scripts check that /dev/i2c-1 exists.
install() redirects those paths (and only those) to a scratch directory that
mirrors them, by wrapping open(), os.open/stat/lstat/listdir/scandir/access.
Dynamic files, such as a DS18B20's w1_slave, are regenerated on every open,
as reading the real file runs a conversion.
"""

from __future__ import annotations

import builtins
import io
import os
import shutil
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

Content = bytes | Callable[[], bytes]

_lock = threading.RLock()
_root: str | None = None
_files: dict[str, Content] = {}  # virtual path → contents, or what produces them
_virtual_roots: set[str] = set()  # paths whose whole subtree is virtual
_originals: dict[str, Callable] = {}


def add_file(path: str, content: Content | str) -> None:
    """Serve *path*: fixed contents, or a callable run on every open."""
    if isinstance(content, str):
        content = content.encode()
    with _lock:
        _files[path] = content
        if _root is not None:
            _materialise(path, content, generate=False)


def add_root(path: str) -> None:
    """Make every path under *path* virtual, so the host's own files there are hidden."""
    with _lock:
        _virtual_roots.add(path.rstrip("/"))
        if _root is not None:
            os.makedirs(_root + path, exist_ok=True)


def _materialise(path: str, content: Content, generate: bool = True) -> None:  # noqa: FBT001, FBT002
    """Write *path*'s contents to the scratch directory; a dynamic file is only
    generated when *generate* (on open), else left empty so it can be listed."""
    real = _root + path
    _originals.get("makedirs", os.makedirs)(os.path.dirname(real), exist_ok=True)
    if callable(content):
        data = content() if generate else b""
    else:
        data = content
    with _originals.get("open", builtins.open)(real, "wb") as f:
        f.write(data)


def _virtual(path: str) -> str | None:
    """The virtual path *path* names, or None for a path of the host."""
    if _root is None:
        return None
    if path.startswith(_root + "/"):  # a path scandir() / glob handed out
        path = path[len(_root):]
    if path in _files:
        return path
    for root in _virtual_roots:
        if path == root or path.startswith(root + "/"):
            return path
    return None


def _redirect(path, refresh: bool = False):  # noqa: FBT001, FBT002
    """The scratch-directory path for a virtual *path*; other paths unchanged."""
    if isinstance(path, int):
        return path
    try:
        fspath = os.fspath(path)
    except TypeError:
        return path
    as_bytes = isinstance(fspath, bytes)
    text = os.fsdecode(fspath)
    if not text.startswith("/"):
        try:
            text = os.path.join(os.getcwd(), text)
        except OSError:  # the working directory is gone: no virtual file is relative to it
            return path
    text = os.path.normpath(text)
    with _lock:
        virtual = _virtual(text)
        if virtual is None:
            return path
        if refresh and callable(_files.get(virtual)):
            _materialise(virtual, _files[virtual])
        real = _root + virtual
    return os.fsencode(real) if as_bytes else real


class _Entry:
    """A scandir() entry whose path is the virtual one, not the scratch directory's."""

    def __init__(self, entry: os.DirEntry, parent: str) -> None:
        self._entry = entry
        self.name = entry.name
        self.path = os.path.join(parent, entry.name)

    def __fspath__(self) -> str:
        return self.path

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        return self._entry.is_dir(follow_symlinks=follow_symlinks)

    def is_file(self, *, follow_symlinks: bool = True) -> bool:
        return self._entry.is_file(follow_symlinks=follow_symlinks)

    def is_symlink(self) -> bool:
        return self._entry.is_symlink()

    def is_junction(self) -> bool:
        return False

    def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
        return self._entry.stat(follow_symlinks=follow_symlinks)

    def inode(self) -> int:
        return self._entry.inode()

    def __repr__(self) -> str:
        return f"<DirEntry {self.name!r}>"


class _Scandir:
    def __init__(self, it, parent: str) -> None:
        self._it = it
        self._parent = parent

    def __iter__(self):
        return self

    def __next__(self) -> _Entry:
        return _Entry(next(self._it), self._parent)

    def __enter__(self) -> _Scandir:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._it.close()


def _open(file, *args, **kwargs):
    return _originals["open"](_redirect(file, refresh=True), *args, **kwargs)


def _os_open(path, *args, **kwargs):
    return _originals["os.open"](_redirect(path, refresh=True), *args, **kwargs)


def _stat(path, *args, **kwargs):
    return _originals["stat"](_redirect(path), *args, **kwargs)


def _lstat(path, *args, **kwargs):
    return _originals["lstat"](_redirect(path), *args, **kwargs)


def _access(path, *args, **kwargs):
    return _originals["access"](_redirect(path), *args, **kwargs)


def _listdir(path="."):
    return _originals["listdir"](_redirect(path))


def _scandir(path="."):
    real = _redirect(path)
    it = _originals["scandir"](real)
    if real is path or isinstance(real, int):
        return it
    return _Scandir(it, os.fsdecode(os.fspath(path)))


def install() -> str:
    """Start serving the virtual files; returns the scratch directory."""
    global _root
    with _lock:
        if _root is not None:
            return _root
        _originals.update({
            "open": builtins.open, "os.open": os.open, "stat": os.stat, "lstat": os.lstat,
            "access": os.access, "listdir": os.listdir, "scandir": os.scandir,
            "makedirs": os.makedirs,
        })
        _root = tempfile.mkdtemp(prefix="p4n4-emu-vfs-")
        for path in _virtual_roots:
            os.makedirs(_root + path, exist_ok=True)
        for path, content in _files.items():
            _materialise(path, content, generate=False)
        builtins.open = io.open = _open
        os.open, os.stat, os.lstat, os.access = _os_open, _stat, _lstat, _access
        os.listdir, os.scandir = _listdir, _scandir
        return _root


def uninstall() -> None:
    """Stop serving them and remove the scratch directory (tests)."""
    global _root
    with _lock:
        if _root is None:
            return
        builtins.open = io.open = _originals["open"]
        os.open, os.stat, os.lstat = _originals["os.open"], _originals["stat"], _originals["lstat"]
        os.access, os.listdir = _originals["access"], _originals["listdir"]
        os.scandir = _originals["scandir"]
        shutil.rmtree(_root, ignore_errors=True)
        _root = None
        _files.clear()
        _virtual_roots.clear()


def root() -> Path | None:
    return Path(_root) if _root else None
