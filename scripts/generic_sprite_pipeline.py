"""Confined offline preparation for generic sprite runs."""

import argparse
import copy
import ctypes
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Callable
import uuid
import zlib

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))

from generic_sprite_contract import build_manifest
from generic_sprite_contract import authorization_contract_sha256
from generic_sprite_contract import authorization_summary
from generic_sprite_contract import canonical_sha256
from generic_sprite_contract import manifest_schema_version
from generic_sprite_contract import validate_movement_plan
from generic_sprite_contract import validate_pricing_evidence
from generic_sprite_contract import _multiply_decimal_exact
from generic_sprite_contract import _sum_decimals_exact
from generic_sprite_images import GenericImageBlocked
from generic_sprite_images import _loop_gif_bytes
from generic_sprite_images import _normalize_generated_frame_bytes
from generic_sprite_images import assemble_sheet
from generic_sprite_images import normalize_generated_frame
from generic_sprite_images import normalize_source
from generic_sprite_images import technical_frame_report


FILE_ATTRIBUTE_REPARSE_POINT = 0x400
GENERIC_READ = 0x80000000
DELETE_ACCESS = 0x00010000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_RENAME_INFO_CLASS = 3
FILE_RENAME_INFO_EX_CLASS = 22
FILE_RENAME_FLAG_REPLACE_IF_EXISTS = 0x00000001
FILE_RENAME_FLAG_POSIX_SEMANTICS = 0x00000002
RENAME_NOREPLACE = 1
AT_FDCWD = -100


@dataclass(frozen=True)
class _Identity:
    device: int
    inode: int
    file_type: int
    attributes: int


@dataclass(frozen=True)
class _FileSnapshot:
    identity: _Identity
    size: int
    modified_ns: int
    digest: str


_REWRITE_SNAPSHOTS: ContextVar[tuple[tuple[Path, _FileSnapshot], ...]] = ContextVar(
    "generic_sprite_rewrite_snapshots", default=()
)


@contextmanager
def _bind_rewrite_snapshot(path: Path, snapshot: _FileSnapshot):
    """Require this target identity in the current scoped invocation only."""
    target = Path(os.path.abspath(path))
    token = _REWRITE_SNAPSHOTS.set(_REWRITE_SNAPSHOTS.get() + ((target, snapshot),))
    try:
        yield
    finally:
        _REWRITE_SNAPSHOTS.reset(token)


@dataclass(frozen=True)
class _WindowsHandleIdentity:
    volume_serial: int
    file_index: int
    attributes: int


@dataclass(frozen=True)
class _TreeRecord:
    relative_path: str
    kind: str
    identity: _Identity
    size: int
    modified_ns: int
    digest: str


def _identity(value: os.stat_result) -> _Identity:
    return _Identity(
        device=value.st_dev,
        inode=value.st_ino,
        file_type=stat.S_IFMT(value.st_mode),
        attributes=getattr(value, "st_file_attributes", 0),
    )


def _is_reparse(value: os.stat_result) -> bool:
    return bool(getattr(value, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def _inside(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve(strict=False).relative_to(Path(root).resolve(strict=False))
        return True
    except ValueError:
        return False


def _safe_chain(path: Path) -> Path:
    """Reject dangling links, symlinks, junctions, and other reparse ancestors."""
    absolute = Path(os.path.abspath(path))
    current = absolute
    while True:
        if os.path.lexists(str(current)):
            if current.is_symlink():
                raise ValueError("LINKED_PATH")
            if hasattr(current, "is_junction") and current.is_junction():
                raise ValueError("REPARSE_PATH")
            try:
                metadata = os.lstat(str(current))
            except FileNotFoundError:
                raise ValueError("PATH_IDENTITY_CHANGED") from None
            if _is_reparse(metadata):
                raise ValueError("REPARSE_PATH")
        if current.parent == current:
            break
        current = current.parent
    return absolute


def _directory_identity(path: Path) -> _Identity:
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError("LINKED_PATH")
    if _is_reparse(metadata):
        raise ValueError("REPARSE_PATH")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("DIRECTORY_REQUIRED")
    return _identity(metadata)


def _file_snapshot(path: Path, data: bytes) -> _FileSnapshot:
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
        raise ValueError("LINKED_PATH")
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError("ARTIFACT_IDENTITY_INVALID")
    return _FileSnapshot(
        identity=_identity(metadata),
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        digest=hashlib.sha256(data).hexdigest(),
    )


def _open_windows_handle(path: Path, access: int, share: int, directory: bool) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    flags = FILE_FLAG_OPEN_REPARSE_POINT
    if directory:
        flags |= FILE_FLAG_BACKUP_SEMANTICS
    handle = create_file(
        str(path),
        access,
        share,
        None,
        OPEN_EXISTING,
        flags,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        error = ctypes.get_last_error()
        raise OSError(error, os.strerror(error), str(path))
    return int(handle)


def _close_windows_handle(handle: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int
    if not close_handle(ctypes.c_void_p(handle)):
        error = ctypes.get_last_error()
        raise OSError(error, os.strerror(error))


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = (
        ("file_attributes", ctypes.c_uint32),
        ("creation_time_low", ctypes.c_uint32),
        ("creation_time_high", ctypes.c_uint32),
        ("last_access_time_low", ctypes.c_uint32),
        ("last_access_time_high", ctypes.c_uint32),
        ("last_write_time_low", ctypes.c_uint32),
        ("last_write_time_high", ctypes.c_uint32),
        ("volume_serial", ctypes.c_uint32),
        ("file_size_high", ctypes.c_uint32),
        ("file_size_low", ctypes.c_uint32),
        ("number_of_links", ctypes.c_uint32),
        ("file_index_high", ctypes.c_uint32),
        ("file_index_low", ctypes.c_uint32),
    )


def _windows_handle_identity(handle: int) -> _WindowsHandleIdentity:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(_ByHandleFileInformation),
    )
    get_information.restype = ctypes.c_int
    information = _ByHandleFileInformation()
    if not get_information(ctypes.c_void_p(handle), ctypes.byref(information)):
        error = ctypes.get_last_error()
        raise OSError(error, os.strerror(error))
    return _WindowsHandleIdentity(
        volume_serial=information.volume_serial,
        file_index=(information.file_index_high << 32) | information.file_index_low,
        attributes=information.file_attributes,
    )


def _windows_path_identity(path: Path, directory: bool) -> _WindowsHandleIdentity:
    handle = _open_windows_handle(
        path,
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        directory,
    )
    try:
        return _windows_handle_identity(handle)
    finally:
        _close_windows_handle(handle)


class _FileRenameInfo(ctypes.Structure):
    _fields_ = (
        ("replace_if_exists", ctypes.c_ubyte),
        ("root_directory", ctypes.c_void_p),
        ("file_name_length", ctypes.c_uint32),
        ("file_name", ctypes.c_wchar * 1),
    )


class _FileRenameInfoEx(ctypes.Structure):
    _fields_ = (
        ("flags", ctypes.c_uint32),
        ("root_directory", ctypes.c_void_p),
        ("file_name_length", ctypes.c_uint32),
        ("file_name", ctypes.c_wchar * 1),
    )


def _build_windows_rename_info(
    destination: Path,
    replace_if_exists: bool = False,
) -> tuple[ctypes.Array, int]:
    destination = Path(destination)
    name = str(destination)
    if not destination.is_absolute() or not name or "\0" in name:
        raise ValueError("PUBLISH_DESTINATION_INVALID")
    encoded = name.encode("utf-16-le")
    if not encoded or len(encoded) > 0xFFFFFFFF:
        raise ValueError("PUBLISH_DESTINATION_INVALID")
    # FileNameLength excludes the terminator, while the supplied buffer retains it.
    structure = _FileRenameInfoEx if replace_if_exists else _FileRenameInfo
    size = structure.file_name.offset + len(encoded) + ctypes.sizeof(ctypes.c_wchar)
    pointer_size = ctypes.sizeof(ctypes.c_void_p)
    storage = (ctypes.c_void_p * ((size + pointer_size - 1) // pointer_size))()
    information = ctypes.cast(storage, ctypes.POINTER(structure)).contents
    if replace_if_exists:
        information.flags = (
            FILE_RENAME_FLAG_REPLACE_IF_EXISTS | FILE_RENAME_FLAG_POSIX_SEMANTICS
        )
    else:
        information.replace_if_exists = 0
    information.root_directory = None
    information.file_name_length = len(encoded)
    ctypes.memmove(
        ctypes.addressof(storage) + structure.file_name.offset,
        encoded,
        len(encoded),
    )
    return storage, size


def _rename_windows_handle(
    handle: int,
    destination: Path,
    replace_if_exists: bool = False,
) -> None:
    buffer, size = _build_windows_rename_info(destination, replace_if_exists)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    )
    set_information.restype = ctypes.c_int
    if not set_information(
        ctypes.c_void_p(handle),
        FILE_RENAME_INFO_EX_CLASS if replace_if_exists else FILE_RENAME_INFO_CLASS,
        buffer,
        size,
    ):
        error = ctypes.get_last_error()
        if not replace_if_exists and (
            error in (80, 183) or (error == 5 and os.path.lexists(destination))
        ):
            raise FileExistsError(error, os.strerror(error), str(destination))
        raise OSError(error, os.strerror(error), str(destination))


class _PathGuard:
    """Keep one filesystem object stable while its path is in use.

    Windows handles deny write/delete sharing. POSIX descriptors preserve the
    opened identity but cannot prevent a concurrent rename, so every operation
    also checks the path identity immediately before and after use.
    """

    def __init__(
        self,
        path: Path,
        directory: bool,
        writable_rename: bool = False,
        allow_delete_share: bool = False,
        expected_snapshot: _FileSnapshot | None = None,
    ):
        self.path = Path(path)
        self.directory = directory
        self.identity = _directory_identity(self.path) if directory else _identity(os.lstat(self.path))
        self.expected_snapshot = expected_snapshot
        self.replacement_expected: tuple[bytes, _FileSnapshot] | None = None
        self.children: list[_PathGuard] = []
        if os.name == "nt":
            access = (
                GENERIC_READ | DELETE_ACCESS
                if writable_rename
                else GENERIC_READ
            )
            share = FILE_SHARE_READ | FILE_SHARE_WRITE if directory else FILE_SHARE_READ
            if allow_delete_share:
                share |= FILE_SHARE_DELETE
            self.handle = _open_windows_handle(self.path, access, share, directory)
            try:
                opened = _windows_handle_identity(self.handle)
                post_open = (
                    _directory_identity(self.path)
                    if directory
                    else _identity(os.lstat(self.path))
                )
                # Only the observed transient attribute may differ on open.
                # Never adopt a replacement object while normalizing that bit.
                stable_attributes = ~0x10000000
                if (
                    post_open.device != self.identity.device
                    or post_open.inode != self.identity.inode
                    or post_open.file_type != self.identity.file_type
                    or (post_open.attributes ^ self.identity.attributes) & stable_attributes
                    # Python exposes the full volume serial; this Win32 API
                    # exposes its low DWORD. Preserve the full path comparison.
                    or (post_open.device & 0xFFFFFFFF) != opened.volume_serial
                    or post_open.inode != opened.file_index
                    or post_open.file_type != (stat.S_IFDIR if opened.attributes & 0x10 else stat.S_IFREG)
                    or (post_open.attributes ^ opened.attributes) & stable_attributes
                ):
                    raise ValueError("PATH_IDENTITY_CHANGED")
                self.identity = post_open
                self.handle_identity = opened
            except Exception:
                _close_windows_handle(self.handle)
                self.handle = None
                raise
        else:
            flags = os.O_RDONLY
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            if directory:
                flags |= getattr(os, "O_DIRECTORY", 0)
            self.handle = os.open(self.path, flags)
        try:
            self.verify()
        except Exception:
            self.close()
            raise

    def verify(self) -> None:
        _safe_chain(self.path)
        current = _directory_identity(self.path) if self.directory else _identity(os.lstat(self.path))
        if current != self.identity:
            raise ValueError("PATH_IDENTITY_CHANGED")
        if os.name == "nt":
            if self.handle is None:
                raise ValueError("PATH_IDENTITY_CHANGED")
            opened = _windows_handle_identity(self.handle)
            named = _windows_path_identity(self.path, self.directory)
            if opened != self.handle_identity or named != self.handle_identity:
                raise ValueError("PATH_IDENTITY_CHANGED")
        else:
            opened = _identity(os.fstat(self.handle))
            if opened != self.identity:
                raise ValueError("PATH_IDENTITY_CHANGED")

    def verify_tree(self) -> None:
        self.verify()
        for child in self.children:
            child.verify()

    def add_child(self, child: "_PathGuard") -> None:
        self.children.append(child)

    def close_children(self) -> None:
        for child in reversed(self.children):
            child.close()
        self.children.clear()

    def release_handle(self) -> None:
        handle = getattr(self, "handle", None)
        if handle is None:
            return
        self.handle = None
        if os.name == "nt":
            _close_windows_handle(handle)
        else:
            os.close(handle)

    def close(self) -> None:
        self.close_children()
        self.release_handle()

    def __enter__(self) -> "_PathGuard":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def _ensure_workspace_root(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    missing: list[Path] = []
    current = absolute
    while not os.path.lexists(current):
        missing.append(current)
        if current.parent == current:
            raise ValueError("WORKSPACE_ROOT_INVALID")
        current = current.parent
    _safe_chain(current)
    if not current.is_dir():
        raise ValueError("WORKSPACE_ROOT_INVALID")
    for candidate in reversed(missing):
        _safe_chain(candidate.parent)
        os.mkdir(candidate)
        _safe_chain(candidate)
        _directory_identity(candidate)
    return absolute


def _verify_operation(root_guard: _PathGuard, stage_guard: _PathGuard) -> None:
    root_guard.verify()
    stage_guard.verify_tree()


def _mkdir_component(
    path: Path,
    root_guard: _PathGuard,
    stage_guard: _PathGuard,
) -> None:
    _verify_operation(root_guard, stage_guard)
    parent_guard = next(
        (guard for guard in (stage_guard, *stage_guard.children) if guard.path == path.parent),
        None,
    )
    if parent_guard is None:
        raise ValueError("PATH_IDENTITY_CHANGED")
    parent_guard.verify()
    os.mkdir(path)
    child = _PathGuard(path, directory=True)
    stage_guard.add_child(child)
    _verify_operation(root_guard, stage_guard)


def _parent_guard(path: Path, stage_guard: _PathGuard) -> _PathGuard:
    for guard in (stage_guard, *stage_guard.children):
        if guard.path == path.parent:
            return guard
    raise ValueError("PATH_IDENTITY_CHANGED")


def _exclusive_bytes(
    path: Path,
    data: bytes,
    root_guard: _PathGuard,
    stage_guard: _PathGuard,
) -> None:
    _verify_operation(root_guard, stage_guard)
    path.relative_to(stage_guard.path)
    _parent_guard(path, stage_guard).verify()
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
        opened = _identity(os.fstat(stream.fileno()))
    snapshot = _file_snapshot(path, data)
    if opened != snapshot.identity or snapshot.size != len(data):
        raise ValueError("ARTIFACT_IDENTITY_INVALID")
    file_guard = _PathGuard(path, directory=False, expected_snapshot=snapshot)
    stage_guard.add_child(file_guard)
    _verify_operation(root_guard, stage_guard)


def _atomic(
    path: Path,
    value: object,
    root_guard: _PathGuard,
    stage_guard: _PathGuard,
) -> None:
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    _exclusive_bytes(path, payload, root_guard, stage_guard)


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def _read_stable_bytes(path: Path, mutation_reason: str = "INPUT_MUTATED") -> bytes:
    absolute = _safe_chain(path)
    before = os.lstat(absolute)
    if stat.S_ISLNK(before.st_mode) or _is_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise ValueError("INPUT_PATH_INVALID")
    with absolute.open("rb") as stream:
        opened_before = os.fstat(stream.fileno())
        data = stream.read()
        opened_after = os.fstat(stream.fileno())
    after = os.lstat(absolute)
    _safe_chain(absolute)
    stable = (
        _identity(before) == _identity(opened_before)
        and _identity(opened_before) == _identity(opened_after)
        and _identity(opened_after) == _identity(after)
        and before.st_size == opened_before.st_size == opened_after.st_size == after.st_size
        and before.st_mtime_ns == opened_after.st_mtime_ns == after.st_mtime_ns
    )
    if not stable:
        raise ValueError(mutation_reason)
    return data


def _read_single_link_bytes(path: Path, reason: str) -> bytes:
    data = _read_stable_bytes(path, reason)
    try:
        metadata = os.lstat(path)
    except OSError:
        raise ValueError(reason) from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or _is_reparse(metadata)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise ValueError(reason)
    return data


def _decode_json(data: bytes, reason: str) -> object:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError(reason) from None


def _write_temp_payload(stream, payload: bytes, target: Path) -> None:
    written = stream.write(payload)
    if written != len(payload):
        raise OSError("SHORT_TEMP_WRITE")


def _read_windows_handle_bytes(handle: int) -> bytes:
    """Read one guarded file through its existing Windows handle."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_pointer = kernel32.SetFilePointerEx
    set_pointer.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int64,
        ctypes.POINTER(ctypes.c_int64),
        ctypes.c_uint32,
    )
    set_pointer.restype = ctypes.c_int
    read_file = kernel32.ReadFile
    read_file.argtypes = (
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    )
    read_file.restype = ctypes.c_int

    def seek_start() -> None:
        position = ctypes.c_int64()
        if not set_pointer(
            ctypes.c_void_p(handle),
            0,
            ctypes.byref(position),
            0,
        ):
            error = ctypes.get_last_error()
            raise OSError(error, os.strerror(error))

    seek_start()
    chunks: list[bytes] = []
    buffer = ctypes.create_string_buffer(64 * 1024)
    while True:
        count = ctypes.c_uint32()
        if not read_file(
            ctypes.c_void_p(handle),
            buffer,
            len(buffer),
            ctypes.byref(count),
            None,
        ):
            error = ctypes.get_last_error()
            raise OSError(error, os.strerror(error))
        if count.value == 0:
            break
        chunks.append(buffer.raw[: count.value])
    seek_start()
    return b"".join(chunks)


def _read_guard_bytes(guard: _PathGuard, reason: str) -> bytes:
    """Read via the same descriptor/handle whose identity is guarded."""
    try:
        if os.name == "nt":
            return _read_windows_handle_bytes(guard.handle)
        os.lseek(guard.handle, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(guard.handle, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        os.lseek(guard.handle, 0, os.SEEK_SET)
        return b"".join(chunks)
    except OSError:
        raise ValueError(reason) from None


def _replace_existing_file(replacement_guard: _PathGuard, target: Path) -> None:
    """Guard the expected destination, quarantine it, then publish no-replace."""
    if os.name == "nt":
        if replacement_guard.replacement_expected is None:
            raise ValueError("PATH_IDENTITY_CHANGED")
        expected, expected_snapshot = replacement_guard.replacement_expected
        with _PathGuard(target.parent, directory=True) as parent_guard:
            with _PathGuard(target, directory=False, writable_rename=True) as target_guard:
                current = _read_guard_bytes(target_guard, "PATH_IDENTITY_CHANGED")
                if current != expected or not _snapshots_match(
                    expected_snapshot, _file_snapshot(target, current)
                ):
                    raise ValueError("PATH_IDENTITY_CHANGED")
                replacement_guard.verify()
                quarantine = target.parent / f".{target.name}.{uuid.uuid4().hex}.rollback"
                # The original target stays locked through its same-handle move.
                _rename_windows_handle(target_guard.handle, quarantine)
                target_guard.path = quarantine
                try:
                    parent_guard.verify()
                    target_guard.verify()
                    _rename_windows_handle(replacement_guard.handle, target)
                except Exception:
                    # Restore only into an absent name. Keep both foreign bytes
                    # and the quarantined original if a concurrent preplant won.
                    try:
                        parent_guard.verify()
                        target_guard.verify()
                        _rename_windows_handle(target_guard.handle, target)
                        target_guard.path = target
                    except (OSError, ValueError):
                        raise ValueError("PATH_IDENTITY_CHANGED") from None
                    raise
                replacement_guard.path = target
                parent_guard.verify()
                replacement_guard.verify()
                _delete_windows_guarded_file(target_guard)
    else:
        # POSIX rename is atomic; verify the live source fd/path at the last
        # possible point, then let the caller revalidate that fd after rename.
        replacement_guard.verify()
        os.replace(replacement_guard.path, target)


def _delete_windows_guarded_file(guard: _PathGuard) -> None:
    """Delete only this open object's name when its guard closes."""
    guard.verify()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = (
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
    )
    set_information.restype = ctypes.c_int
    delete = ctypes.c_ubyte(1)
    if not set_information(
        ctypes.c_void_p(guard.handle), 4, ctypes.byref(delete), ctypes.sizeof(delete)
    ):
        error = ctypes.get_last_error()
        raise OSError(error, os.strerror(error), str(guard.path))


def _rewrite_existing_json(path: Path, value: object, expected: bytes) -> _FileSnapshot:
    """Publish complete JSON; Windows guards, quarantines, then creates no-replace.

    A failed publication restores the original by its live handle if the name
    is still absent. A foreign preplant is preserved, alongside the quarantined
    original for recovery. A process cut between renames can retain quarantine;
    this is a recoverable transition, not a filesystem transaction.
    """
    path = Path(os.path.abspath(path))
    parent = _safe_chain(path.parent)
    payload = _json_bytes(value)
    with _PathGuard(parent, directory=True) as parent_guard:
        current = _read_single_link_bytes(path, "ARTIFACT_IDENTITY_INVALID")
        if current != expected:
            raise ValueError("PATH_IDENTITY_CHANGED")
        observed_snapshot = _file_snapshot(path, current)
        target_snapshot = next(
            (snapshot for target, snapshot in reversed(_REWRITE_SNAPSHOTS.get()) if target == path),
            observed_snapshot,
        )
        if not _snapshots_match(target_snapshot, observed_snapshot):
            raise ValueError("PATH_IDENTITY_CHANGED")
        with _PathGuard(
            path,
            directory=False,
            allow_delete_share=True,
            expected_snapshot=target_snapshot,
        ) as target_guard:
            parent_guard.verify()
            target_guard.verify()
            if not _identities_match(target_snapshot.identity, target_guard.identity):
                raise ValueError("PATH_IDENTITY_CHANGED")
            temporary = parent / f".{path.name}.{uuid.uuid4().hex}.partial"
            with temporary.open("xb") as stream:
                _write_temp_payload(stream, payload, path)
                stream.flush()
                os.fsync(stream.fileno())
            temporary_bytes = _read_single_link_bytes(
                temporary,
                "ARTIFACT_IDENTITY_INVALID",
            )
            if temporary_bytes != payload:
                raise ValueError("ARTIFACT_IDENTITY_INVALID")
            temporary_snapshot = _file_snapshot(temporary, temporary_bytes)
            with _PathGuard(
                temporary,
                directory=False,
                writable_rename=True,
                expected_snapshot=temporary_snapshot,
            ) as temporary_guard:
                parent_guard.verify()
                target_guard.verify()
                temporary_guard.verify()
                if not _identities_match(temporary_snapshot.identity, temporary_guard.identity):
                    raise ValueError("PATH_IDENTITY_CHANGED")
                if _read_guard_bytes(target_guard, "PATH_IDENTITY_CHANGED") != expected:
                    raise ValueError("PATH_IDENTITY_CHANGED")
                if _read_guard_bytes(temporary_guard, "PATH_IDENTITY_CHANGED") != payload:
                    raise ValueError("PATH_IDENTITY_CHANGED")
                temporary_guard.replacement_expected = (expected, target_snapshot)
                _replace_existing_file(temporary_guard, path)
                temporary_guard.path = path
                parent_guard.verify()
                temporary_guard.verify()
                published = _read_guard_bytes(temporary_guard, "PATH_IDENTITY_CHANGED")
                if published != payload:
                    raise ValueError("PATH_IDENTITY_CHANGED")
                published_snapshot = _file_snapshot(path, published)
                if published_snapshot.identity != temporary_snapshot.identity:
                    raise ValueError("PATH_IDENTITY_CHANGED")
    return published_snapshot


def _assert_source_unchanged(
    path: Path,
    expected: bytes,
    reason: str,
    root_guard: _PathGuard,
    stage_guard: _PathGuard,
) -> None:
    _verify_operation(root_guard, stage_guard)
    current = _read_stable_bytes(path, reason)
    _verify_operation(root_guard, stage_guard)
    if current != expected:
        raise ValueError(reason)


def _publish_linux_no_replace(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise ValueError("ATOMIC_PUBLISH_UNSUPPORTED")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        AT_FDCWD,
        os.fsencode(source),
        AT_FDCWD,
        os.fsencode(destination),
        RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == 17:
        raise FileExistsError(error, os.strerror(error), str(destination))
    raise OSError(error, os.strerror(error), str(destination))


def _snapshot_tree(root: Path) -> tuple[_TreeRecord, ...]:
    records = []
    pending = [Path(root)]
    while pending:
        parent = pending.pop()
        try:
            with os.scandir(parent) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError:
            raise ValueError("PUBLISHED_TREE_CHANGED") from None
        directories = []
        for entry in entries:
            path = parent / entry.name
            relative = path.relative_to(root).as_posix()
            try:
                metadata = os.lstat(path)
                if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
                    raise ValueError("PUBLISHED_TREE_CHANGED")
                identity = _identity(metadata)
                if stat.S_ISDIR(metadata.st_mode):
                    records.append(_TreeRecord(relative, "directory", identity, 0, 0, ""))
                    directories.append(path)
                elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                    data = _read_stable_bytes(path, "PUBLISHED_TREE_CHANGED")
                    snapshot = _file_snapshot(path, data)
                    records.append(
                        _TreeRecord(
                            relative,
                            "file",
                            snapshot.identity,
                            snapshot.size,
                            snapshot.modified_ns,
                            snapshot.digest,
                        )
                    )
                else:
                    raise ValueError("PUBLISHED_TREE_CHANGED")
            except (OSError, ValueError):
                raise ValueError("PUBLISHED_TREE_CHANGED") from None
        pending.extend(reversed(directories))
    return tuple(sorted(records, key=lambda record: record.relative_path))


def _tree_records_match(
    expected: tuple[_TreeRecord, ...], current: tuple[_TreeRecord, ...]
) -> bool:
    # Windows can toggle this transient attribute while publishing a directory.
    # Keep inventory, stable identity attributes, and all content evidence exact.
    return len(expected) == len(current) and all(
        first.relative_path == second.relative_path
        and first.kind == second.kind
        and _identities_match(first.identity, second.identity)
        and first.size == second.size
        and first.modified_ns == second.modified_ns
        and first.digest == second.digest
        for first, second in zip(expected, current)
    )


def _seal_guarded_tree(stage_guard: _PathGuard) -> tuple[_TreeRecord, ...]:
    stage_guard.verify_tree()
    actual = _snapshot_tree(stage_guard.path)
    expected = []
    for child in stage_guard.children:
        relative = child.path.relative_to(stage_guard.path).as_posix()
        if child.directory:
            expected.append(_TreeRecord(relative, "directory", child.identity, 0, 0, ""))
        else:
            snapshot = child.expected_snapshot
            if snapshot is None:
                try:
                    data = _read_stable_bytes(child.path, "PUBLISHED_TREE_CHANGED")
                    snapshot = _file_snapshot(child.path, data)
                except (OSError, ValueError):
                    raise ValueError("PUBLISHED_TREE_CHANGED") from None
            expected.append(
                _TreeRecord(
                    relative,
                    "file",
                    snapshot.identity,
                    snapshot.size,
                    snapshot.modified_ns,
                    snapshot.digest,
                )
            )
    expected_value = tuple(sorted(expected, key=lambda record: record.relative_path))
    if not _tree_records_match(expected_value, actual):
        raise ValueError("PUBLISHED_TREE_CHANGED")
    return actual


def _publish_stage(
    stage_guard: _PathGuard,
    root_guard: _PathGuard,
    destination: Path,
) -> None:
    _verify_operation(root_guard, stage_guard)
    _safe_chain(destination.parent)
    if os.path.lexists(destination):
        raise ValueError("RUN_EXISTS")
    seal = _seal_guarded_tree(stage_guard)
    stage_guard.close_children()
    root_guard.verify()
    stage_guard.verify()
    try:
        if os.name == "nt":
            _rename_windows_handle(stage_guard.handle, destination)
        elif sys.platform.startswith("linux"):
            _publish_linux_no_replace(stage_guard.path, destination)
        else:
            raise ValueError("ATOMIC_PUBLISH_UNSUPPORTED")
    except FileExistsError:
        raise ValueError("RUN_EXISTS") from None
    old_path = stage_guard.path
    stage_guard.path = destination
    try:
        root_guard.verify()
        stage_guard.verify_tree()
        if not _tree_records_match(seal, _snapshot_tree(destination)):
            raise ValueError("PUBLISHED_TREE_CHANGED")
    except Exception:
        if os.path.lexists(old_path):
            stage_guard.path = old_path
        raise


def prepare_run(
    *,
    source: Path,
    movement_plan_path: Path,
    pricing_path: Path,
    animation: str,
    direction: str,
    frame_count: int,
    frame_size: tuple[int, int] | None,
    max_budget_usd: Decimal,
    workspace_root: Path,
    run_id: str,
) -> Path:
    source = Path(source)
    movement_plan_path = Path(movement_plan_path)
    pricing_path = Path(pricing_path)
    workspace_root = Path(workspace_root)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id):
        raise ValueError("RUN_ID_INVALID")
    source_real = _safe_chain(source).resolve(strict=True)
    movement_real = _safe_chain(movement_plan_path).resolve(strict=True)
    pricing_real = _safe_chain(pricing_path).resolve(strict=True)
    root_absolute = Path(os.path.abspath(workspace_root))
    _safe_chain(root_absolute)
    if _inside(root_absolute, source_real.parent):
        raise ValueError("WORKSPACE_INSIDE_SOURCE")
    plan_bytes = _read_stable_bytes(movement_real)
    pricing_bytes = _read_stable_bytes(pricing_real)
    plan_value = json.loads(plan_bytes.decode("utf-8"))
    plan = validate_movement_plan(plan_value, frame_count)
    pricing_value = json.loads(pricing_bytes.decode("utf-8"))
    pricing = validate_pricing_evidence(
        pricing_value,
        "fal-ai/nano-banana-2/edit",
        datetime.now(timezone.utc),
    )
    source_bytes = _read_stable_bytes(source_real, "SOURCE_MUTATED_DURING_COPY")
    source_digest = hashlib.sha256(source_bytes).hexdigest()
    root_real = _ensure_workspace_root(root_absolute)
    with _PathGuard(root_real, directory=True) as root_guard:
        root_guard.verify()
        run_dir = root_real / run_id
        if os.path.lexists(run_dir):
            raise ValueError("RUN_EXISTS")
        stage = root_real / f".{run_id}.{uuid.uuid4().hex}.partial"
        os.mkdir(stage)
        with _PathGuard(stage, directory=True, writable_rename=True) as stage_guard:
            root_guard.verify()
            stage_guard.verify()
            for relative in (
                "input",
                "input/source",
                "input/requests",
                "raw",
                "normalized",
                "normalized/frames",
                "previews",
                "reports",
                "reports/frames",
            ):
                _mkdir_component(stage / relative, root_guard, stage_guard)
            copied = stage / "input/source/original.png"
            _exclusive_bytes(copied, source_bytes, root_guard, stage_guard)
            _assert_source_unchanged(
                source_real,
                source_bytes,
                "SOURCE_MUTATED_DURING_COPY",
                root_guard,
                stage_guard,
            )
            _verify_operation(root_guard, stage_guard)
            with Image.open(copied) as copied_image:
                copied_mode = copied_image.mode
                copied_size = list(copied_image.size)
            _verify_operation(root_guard, stage_guard)
            normalized, report = normalize_source(copied, frame_size)
            _verify_operation(root_guard, stage_guard)
            _assert_source_unchanged(
                source_real,
                source_bytes,
                "SOURCE_MUTATED_AFTER_COPY",
                root_guard,
                stage_guard,
            )
            buffer = io.BytesIO()
            normalized.save(buffer, format="PNG")
            _exclusive_bytes(
                stage / "input/source/master.png",
                buffer.getvalue(),
                root_guard,
                stage_guard,
            )
            master_digest = hashlib.sha256(buffer.getvalue()).hexdigest()
            source_record = {
                "original_path": "input/source/original.png",
                "normalized_path": "input/source/master.png",
                "original_filename": source.name,
                "byte_length": len(source_bytes),
                "sha256": source_digest,
                "normalized_sha256": master_digest,
                "original_size": copied_size,
                "normalized_size": report["normalized_size"],
                "original_mode": copied_mode,
                "background_action": report["background_action"],
                "alpha_bbox": report["alpha_bbox"],
                "resample_mode": report["resample_mode"],
            }
            manifest = build_manifest(
                source_record=source_record,
                animation=animation,
                direction=direction,
                frame_count=frame_count,
                movement_plan=plan,
                frame_size=normalized.size,
                model="fal-ai/nano-banana-2/edit",
                pricing=pricing,
                max_budget_usd=max_budget_usd,
            )
            _atomic(
                stage / "input/movement-plan.json",
                list(plan),
                root_guard,
                stage_guard,
            )
            _atomic(
                stage / "reports/manifest.json",
                manifest,
                root_guard,
                stage_guard,
            )
            lifecycle = {
                "schema_version": manifest["schema_version"],
                "manifest_path": "reports/manifest.json",
                "manifest_sha256": canonical_sha256(manifest),
                "state": "DRAFT",
                "terminal_reason": None,
                "requests": [],
                "frames": [],
                "next_request": 1,
                "artifacts": {},
            }
            _atomic(
                stage / "reports/run_manifest.json",
                lifecycle,
                root_guard,
                stage_guard,
            )
            _publish_stage(stage_guard, root_guard, run_dir)
            return run_dir


def _run_paths(run_manifest_path: Path) -> tuple[Path, Path, Path]:
    run_manifest = Path(os.path.abspath(run_manifest_path))
    _safe_chain(run_manifest)
    if run_manifest.name != "run_manifest.json" or run_manifest.parent.name != "reports":
        raise ValueError("RUN_MANIFEST_PATH_INVALID")
    run_dir = run_manifest.parent.parent
    _safe_chain(run_dir)
    _directory_identity(run_dir)
    manifest = run_dir / "reports/manifest.json"
    return run_dir, run_manifest, manifest


def _read_run_pair(run_manifest_path: Path) -> tuple[Path, Path, bytes, dict, bytes, dict]:
    run_dir, run_path, manifest_path = _run_paths(run_manifest_path)
    run_bytes = _read_single_link_bytes(run_path, "RUN_MANIFEST_INVALID")
    run = _decode_json(run_bytes, "RUN_MANIFEST_INVALID")
    if not isinstance(run, dict) or run.get("manifest_path") != "reports/manifest.json":
        raise ValueError("RUN_MANIFEST_INVALID")
    manifest_bytes = _read_single_link_bytes(manifest_path, "MANIFEST_HASH_MISMATCH")
    manifest = _decode_json(manifest_bytes, "MANIFEST_HASH_MISMATCH")
    if not isinstance(manifest, dict):
        raise ValueError("MANIFEST_HASH_MISMATCH")
    return run_dir, run_path, run_bytes, run, manifest_bytes, manifest


def _validate_schema_pair(run: dict, manifest: dict) -> int:
    version = manifest_schema_version(manifest)
    run_version = run.get("schema_version")
    if (
        type(run_version) is not int
        or run_version not in (1, 2)
        or run_version != version
    ):
        raise ValueError("RUN_MANIFEST_INVALID")
    return version


def _validate_persisted_inputs(run_dir: Path, manifest: dict) -> None:
    try:
        source = manifest["source"]
        original_relative = source["original_path"]
        original_sha256 = source["sha256"]
        master_relative = source["normalized_path"]
        master_sha256 = source["normalized_sha256"]
        movement_plan = manifest["movement_plan"]
    except (KeyError, TypeError):
        raise ValueError("MANIFEST_INVALID") from None
    if original_relative != "input/source/original.png" or master_relative != "input/source/master.png":
        raise ValueError("MANIFEST_INVALID")
    original_path = run_dir / original_relative
    original_bytes = _read_single_link_bytes(original_path, "ORIGINAL_HASH_MISMATCH")
    if hashlib.sha256(original_bytes).hexdigest() != original_sha256:
        raise ValueError("ORIGINAL_HASH_MISMATCH")
    movement_path = run_dir / "input/movement-plan.json"
    movement_bytes = _read_single_link_bytes(movement_path, "MOVEMENT_PLAN_MISMATCH")
    if movement_bytes != _json_bytes(movement_plan):
        raise ValueError("MOVEMENT_PLAN_MISMATCH")
    master_path = run_dir / master_relative
    master_bytes = _read_single_link_bytes(master_path, "MASTER_HASH_MISMATCH")
    if hashlib.sha256(master_bytes).hexdigest() != master_sha256:
        raise ValueError("MASTER_HASH_MISMATCH")


def _validate_authorized_manifest(manifest: dict) -> None:
    try:
        authorization = manifest["authorization"]
    except (KeyError, TypeError):
        raise ValueError("MANIFEST_INVALID") from None
    required = {
        "status",
        "planned_requests",
        "worst_case_usd",
        "max_budget_usd",
        "authorized_at",
        "authorized_budget_usd",
        "contract_sha256",
    }
    if not isinstance(authorization, dict) or set(authorization) != required:
        raise ValueError("AUTHORIZATION_RECORD_INVALID")
    if authorization["status"] != "AUTHORIZED":
        raise ValueError("RUN_NOT_AUTHORIZED")
    if authorization["authorized_budget_usd"] != authorization["max_budget_usd"]:
        raise ValueError("AUTHORIZED_BUDGET_MISMATCH")
    try:
        authorized_at = datetime.fromisoformat(
            str(authorization["authorized_at"]).replace("Z", "+00:00")
        )
    except ValueError:
        raise ValueError("AUTHORIZATION_RECORD_INVALID") from None
    if authorized_at.tzinfo is None or authorized_at.utcoffset() != timezone.utc.utcoffset(authorized_at):
        raise ValueError("AUTHORIZATION_RECORD_INVALID")
    contract_hash = authorization["contract_sha256"]
    if (
        not isinstance(contract_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", contract_hash) is None
        or contract_hash != authorization_contract_sha256(manifest)
    ):
        raise ValueError("AUTHORIZATION_RECORD_INVALID")


def _load_verified_run(run_manifest_path: Path, require_authorized: bool) -> tuple[Path, dict, dict]:
    run_dir, _run_path, _run_bytes, run, _manifest_bytes, manifest = _read_run_pair(
        run_manifest_path
    )
    _validate_schema_pair(run, manifest)
    expected_hash = run.get("manifest_sha256")
    if expected_hash != canonical_sha256(manifest):
        raise ValueError("MANIFEST_HASH_MISMATCH")
    _validate_persisted_inputs(run_dir, manifest)
    authorized_states = {
        "AUTHORIZED",
        "TECHNICAL_REJECTED",
        "READY_FOR_VISUAL_REVIEW",
        "APPROVED",
        "APPROVED_WITH_CORRECTIONS",
        "REJECTED",
    }
    if run.get("state") in authorized_states:
        _validate_run_identity(run_dir, run)
    if require_authorized:
        if run.get("state") not in authorized_states:
            raise ValueError("RUN_NOT_AUTHORIZED")
        _validate_authorized_manifest(manifest)
    return run_dir, run, manifest


def run_authorization_summary(run_manifest_path: Path) -> dict:
    _run_dir, _run, manifest = _load_verified_run(run_manifest_path, False)
    return authorization_summary(manifest)


def _budget_matches(authorization: dict, authorized_budget_usd: Decimal) -> bool:
    if not isinstance(authorized_budget_usd, Decimal) or not authorized_budget_usd.is_finite():
        return False
    try:
        planned = Decimal(str(authorization["max_budget_usd"]))
    except (KeyError, InvalidOperation, TypeError, ValueError):
        return False
    return authorized_budget_usd == planned


def _validate_run_identity(run_dir: Path, run: dict) -> str:
    artifacts = run.get("artifacts")
    if not isinstance(artifacts, dict) or artifacts.get("run_id") != run_dir.name:
        raise ValueError("RUN_IDENTITY_MISMATCH")
    return artifacts["run_id"]


def authorize_run(run_manifest_path: Path, authorized_budget_usd: Decimal) -> dict:
    """Authorize once, or safely finish the lifecycle write after interruption."""
    run_dir, run_path, run_bytes, run, manifest_bytes, manifest = _read_run_pair(
        run_manifest_path
    )
    _validate_schema_pair(run, manifest)
    if run.get("state") not in ("DRAFT", "AUTHORIZED"):
        raise ValueError("RUN_NOT_DRAFT")
    if (
        run.get("state") == "AUTHORIZED"
        and run.get("manifest_sha256") != canonical_sha256(manifest)
    ):
        raise ValueError("MANIFEST_HASH_MISMATCH")
    if run.get("state") == "AUTHORIZED":
        _validate_run_identity(run_dir, run)
    try:
        authorization = manifest["authorization"]
    except (KeyError, TypeError):
        raise ValueError("MANIFEST_INVALID") from None
    if not isinstance(authorization, dict) or not _budget_matches(
        authorization,
        authorized_budget_usd,
    ):
        raise ValueError("AUTHORIZED_BUDGET_MISMATCH")
    _validate_persisted_inputs(run_dir, manifest)

    if authorization.get("status") == "PENDING":
        if run.get("state") != "DRAFT":
            raise ValueError("RUN_NOT_DRAFT")
        if run.get("manifest_sha256") != canonical_sha256(manifest):
            raise ValueError("MANIFEST_HASH_MISMATCH")
        if set(authorization) != {
            "status",
            "planned_requests",
            "worst_case_usd",
            "max_budget_usd",
        }:
            raise ValueError("AUTHORIZATION_RECORD_INVALID")
        authorized_manifest = copy.deepcopy(manifest)
        authorized_manifest["authorization"].update(
            {
                "status": "AUTHORIZED",
                "authorized_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "authorized_budget_usd": authorization["max_budget_usd"],
                "contract_sha256": authorization_contract_sha256(manifest),
            }
        )
        _rewrite_existing_json(
            run_dir / "reports/manifest.json",
            authorized_manifest,
            manifest_bytes,
        )
        final_manifest = authorized_manifest
    elif authorization.get("status") == "AUTHORIZED":
        _validate_authorized_manifest(manifest)
        if authorization.get("authorized_budget_usd") != authorization.get("max_budget_usd"):
            raise ValueError("AUTHORIZED_BUDGET_MISMATCH")
        final_manifest = manifest
        if run.get("state") == "DRAFT":
            prepared = copy.deepcopy(manifest)
            for key in ("authorized_at", "authorized_budget_usd", "contract_sha256"):
                prepared["authorization"].pop(key, None)
            prepared["authorization"]["status"] = "PENDING"
            if run.get("manifest_sha256") != canonical_sha256(prepared):
                raise ValueError("MANIFEST_HASH_MISMATCH")
        elif run.get("manifest_sha256") != canonical_sha256(manifest):
            raise ValueError("MANIFEST_HASH_MISMATCH")
    else:
        raise ValueError("RUN_NOT_DRAFT")

    if run.get("state") == "AUTHORIZED":
        return run
    authorized_run = copy.deepcopy(run)
    authorized_run["state"] = "AUTHORIZED"
    authorized_run["manifest_sha256"] = canonical_sha256(final_manifest)
    if not isinstance(authorized_run.get("artifacts"), dict):
        raise ValueError("RUN_MANIFEST_INVALID")
    authorized_run["artifacts"]["run_id"] = run_dir.name
    _rewrite_existing_json(run_path, authorized_run, run_bytes)
    return authorized_run


def _request_references(
    run_dir: Path,
    run: dict,
    manifest: dict,
    frame_index: int,
) -> tuple[list[dict], list[dict]]:
    references = [{"role": "master", "path": "input/source/master.png"}]
    hashed_references = [
        {
            "role": "master",
            "path": "input/source/master.png",
            "sha256": manifest["source"]["normalized_sha256"],
        }
    ]
    uncertain = {"SUBMISSION_STATUS_UNKNOWN", "REMOTE_STATE_UNCERTAIN", "UNKNOWN"}
    requests = run.get("requests")
    if not isinstance(requests, list):
        raise ValueError("RUN_MANIFEST_INVALID")
    if any(isinstance(item, dict) and item.get("status") in uncertain for item in requests):
        raise ValueError("REMOTE_STATE_UNCERTAIN")
    frames = run.get("frames")
    if not isinstance(frames, list):
        raise ValueError("RUN_MANIFEST_INVALID")
    if any(isinstance(item, dict) and item.get("status") == "REJECTED" for item in frames):
        raise ValueError("PREVIOUS_FRAME_REJECTED")
    expected_indices = set(range(1, frame_index))
    actual_indices = [
        item.get("frame_index")
        for item in frames
        if isinstance(item, dict) and type(item.get("frame_index")) is int
    ]
    if (
        len(actual_indices) != len(frames)
        or len(set(actual_indices)) != len(actual_indices)
        or set(actual_indices) != expected_indices
    ):
        raise ValueError("PREVIOUS_FRAME_MISSING")
    ordered = sorted(frames, key=lambda item: item["frame_index"])
    for item in ordered:
        index = item["frame_index"]
        if item.get("status") != "ACCEPTED":
            raise ValueError("PREVIOUS_FRAME_UNAVAILABLE")
        expected_path = f"normalized/frames/frame-{index:02d}.png"
        if item.get("normalized_path") != expected_path:
            raise ValueError("PREVIOUS_FRAME_MISSING")
        frame_path = run_dir / expected_path
        if not os.path.lexists(frame_path):
            raise ValueError("PREVIOUS_FRAME_MISSING")
        frame_bytes = _read_single_link_bytes(
            frame_path,
            "PREVIOUS_FRAME_HASH_MISMATCH",
        )
        digest = hashlib.sha256(frame_bytes).hexdigest()
        if digest != item.get("normalized_sha256"):
            raise ValueError("PREVIOUS_FRAME_HASH_MISMATCH")
    if frame_index > 1:
        previous = ordered[-1]
        expected_path = previous["normalized_path"]
        references.append({"role": "previous_accepted", "path": expected_path})
        hashed_references.append(
            {
                "role": "previous_accepted",
                "path": expected_path,
                "sha256": previous["normalized_sha256"],
            }
        )
    return references, hashed_references


def _request_fingerprint(
    request_without_fingerprint: dict,
    run_id: str,
    manifest_sha256: str,
    references: list[dict],
) -> str:
    envelope = {
        "schema_version": 1,
        "request": request_without_fingerprint,
        "run_id": run_id,
        "manifest_sha256": manifest_sha256,
        "references": references,
    }
    return canonical_sha256(envelope)


def _persist_request_plan(
    path: Path,
    request: dict,
    requests_root: Path,
    run_dir: Path,
) -> dict:
    supplied = Path(path)
    expected_name = f"frame-{request['frame_index']:02d}.json"
    if ".." in supplied.parts or ":" in supplied.name:
        raise ValueError("REQUEST_PATH_INVALID")
    absolute = Path(os.path.abspath(supplied))
    root = Path(os.path.abspath(requests_root))
    expected = root / expected_name
    if str(absolute) != str(expected) or supplied.name != expected_name:
        raise ValueError("REQUEST_PATH_INVALID")
    payload = _json_bytes(request)
    with _PathGuard(run_dir, directory=True) as run_guard:
        with _PathGuard(root, directory=True) as requests_guard:
            run_guard.verify()
            requests_guard.verify()
            if os.path.lexists(absolute):
                existing = _read_single_link_bytes(absolute, "REQUEST_PATH_INVALID")
                snapshot = _file_snapshot(absolute, existing)
                with _PathGuard(
                    absolute,
                    directory=False,
                    expected_snapshot=snapshot,
                ) as file_guard:
                    run_guard.verify()
                    requests_guard.verify()
                    file_guard.verify()
                    value = _decode_json(existing, "REQUEST_PLAN_INVALID")
                    if (
                        existing != payload
                        or not isinstance(value, dict)
                        or set(value) != {
                            "frame_index",
                            "model",
                            "references",
                            "instructions",
                            "request_fingerprint",
                        }
                    ):
                        raise ValueError("REQUEST_PLAN_INVALID")
                    if _read_single_link_bytes(absolute, "REQUEST_PLAN_INVALID") != payload:
                        raise ValueError("REQUEST_PLAN_INVALID")
                    run_guard.verify()
                    requests_guard.verify()
                    file_guard.verify()
                    return value
            try:
                with absolute.open("xb") as stream:
                    written = stream.write(payload)
                    if written != len(payload):
                        raise OSError("SHORT_REQUEST_WRITE")
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError:
                raise ValueError("REQUEST_PATH_INVALID") from None
            created = _read_single_link_bytes(absolute, "REQUEST_PATH_INVALID")
            created_snapshot = _file_snapshot(absolute, created)
            with _PathGuard(
                absolute,
                directory=False,
                expected_snapshot=created_snapshot,
            ) as file_guard:
                run_guard.verify()
                requests_guard.verify()
                file_guard.verify()
                if created != payload:
                    raise ValueError("REQUEST_PLAN_INVALID")
            return request


def next_request_plan(run_manifest_path: Path, output_path: Path) -> dict | None:
    run_dir, run, manifest = _load_verified_run(run_manifest_path, True)
    next_request = run.get("next_request")
    if run.get("terminal_reason") is not None:
        if run.get("state") == "TECHNICAL_REJECTED":
            return None
        raise ValueError("RUN_TERMINAL")
    frame_index = next_request
    frame_count = manifest.get("request", {}).get("frame_count")
    if type(frame_index) is not int or type(frame_count) is not int or frame_index <= 0:
        raise ValueError("RUN_MANIFEST_INVALID")
    if frame_index > frame_count + 1:
        raise ValueError("RUN_MANIFEST_INVALID")
    references, hashed_references = _request_references(
        run_dir,
        run,
        manifest,
        frame_index,
    )
    if frame_index > 1:
        _revalidate_prior_evaluations(
            run_dir,
            run,
            manifest,
            frame_index - 1,
        )
    if frame_index == frame_count + 1:
        return None
    movement_plan = manifest.get("movement_plan")
    if not isinstance(movement_plan, list) or len(movement_plan) != frame_count:
        raise ValueError("MANIFEST_INVALID")
    movement = movement_plan[frame_index - 1]
    if not isinstance(movement, dict) or movement.get("frame_index") != frame_index:
        raise ValueError("MANIFEST_INVALID")
    request = {
        "frame_index": frame_index,
        "model": manifest["provider"]["model"],
        "references": references,
        "instructions": {
            "animation": manifest["request"]["animation"],
            "direction": manifest["request"]["direction"],
            "phase": movement["phase"],
            "body_motion": movement["body_motion"],
            "identity": "preserve the same character, face, proportions, clothing, equipment, palette and art style",
            "framing": "one complete character, bottom-center grounded, no crop",
            "background": "solid uniform run chroma, no shadow, no gradient",
        },
    }
    run_id = _validate_run_identity(run_dir, run)
    request["request_fingerprint"] = _request_fingerprint(
        request,
        run_id,
        run["manifest_sha256"],
        hashed_references,
    )
    return _persist_request_plan(
        output_path,
        request,
        run_dir / "input/requests",
        run_dir,
    )


_EVALUATION_REPORT_KEYS = {
    "schema_version",
    "frame_index",
    "technical_result",
    "failures",
    "normalization",
    "advisory",
    "provenance",
}
_EVALUATION_CONTENT_FAILURES = {
    "FRAME_EMPTY",
    "FRAME_CLIPPED",
    "FRAME_DUPLICATE",
    "FRAME_NOT_PNG",
}
_EVALUATION_METRIC_DEFINITIONS = {
    "bbox_movement": "candidate_bbox_minus_reference_bbox_[left,top,right,bottom]",
    "normalized_pixel_difference_count": "exact_rgba_pixel_mismatches_on_equal_canvas",
    "silhouette": "alpha_gt_zero_intersection_and_union_pixel_counts",
    "dominant_color_histogram_distance": "integer_l1_distance_over_4bit_rgb_histograms_of_visible_pixels",
}
_EVALUATION_RUN_KEYS = {
    "artifacts",
    "frames",
    "manifest_path",
    "manifest_sha256",
    "next_request",
    "requests",
    "schema_version",
    "state",
    "terminal_reason",
}
_TASK5_REQUEST_BASE_KEYS = {
    "frame_index",
    "attempt",
    "model",
    "request_fingerprint",
    "request_id",
    "status",
    "estimated_cost_usd",
    "submitted_at",
    "result_path",
}
_TASK5_REQUEST_OPTIONAL_KEYS = {"raw_sha256", "safe_error_type"}
_TASK5_SAFE_ERROR_TYPES = {
    "AbortError",
    "FalError",
    "InvalidSubmissionResponse",
    "NetworkError",
    "RESULT_VALIDATION_ERROR",
    "RAW_PROVENANCE_MISMATCH",
    "RESULT_DESTINATION_EXISTS",
    "RESULT_NOT_PNG",
    "RESULT_SCHEMA_INVALID",
    "RESULT_TOO_LARGE",
    "RESULT_URL_NOT_ALLOWED",
    "SubmitError",
    "TimeoutError",
    "WORKSPACE_CONFINEMENT_FAILED",
    "image_too_small",
}
_TASK5_DETERMINISTIC_FAILURES = {
    "RAW_PROVENANCE_MISMATCH",
    "RESULT_DESTINATION_EXISTS",
    "RESULT_NOT_PNG",
    "RESULT_SCHEMA_INVALID",
    "RESULT_TOO_LARGE",
    "RESULT_URL_NOT_ALLOWED",
    "WORKSPACE_CONFINEMENT_FAILED",
}
_TASK5_ACTIVE_STATUSES = {"IN_QUEUE", "IN_PROGRESS"}
_TASK5_REMOTE_TERMINAL_STATUSES = {"FAILED", "CANCELLED"}
_TASK5_KNOWN_STATUSES = {
    "SUBMITTING",
    "SUBMISSION_STATUS_UNKNOWN",
    *_TASK5_ACTIVE_STATUSES,
    "COMPLETED",
    *_TASK5_REMOTE_TERMINAL_STATUSES,
    "REJECTED",
}
_EVALUATION_FRAME_KEYS = {
    "frame_index",
    "status",
    "raw_path",
    "raw_sha256",
    "normalized_path",
    "normalized_sha256",
    "report_path",
    "report_sha256",
}
_EVALUATION_CLAIM_KEYS = {
    "frame_index",
    "raw_path",
    "raw_sha256",
    "normalized_path",
    "normalized_sha256",
    "report_path",
    "report_sha256",
    "technical_result",
    "failure",
    "status",
}
_MAX_RESULT_BYTES = 50 * 1024 * 1024


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _task5_safe_request_id(value: object) -> bool:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or value.strip() != value
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) is None
    ):
        return False
    credential = os.environ.get("FAL_KEY", "").strip()
    return not credential or (credential not in value and value not in credential)


def _task5_structural_png(payload: bytes) -> bool:
    signature = b"\x89PNG\r\n\x1a\n"
    if not signature == payload[:8] or not 8 <= len(payload) <= _MAX_RESULT_BYTES:
        return False
    offset = 8
    chunk_index = 0
    saw_header = False
    saw_data = False
    while offset < len(payload):
        if len(payload) - offset < 12:
            return False
        length = int.from_bytes(payload[offset : offset + 4], "big")
        type_start = offset + 4
        data_start = type_start + 4
        data_end = data_start + length
        end = data_end + 4
        if end > len(payload):
            return False
        kind = payload[type_start:data_start]
        if len(kind) != 4 or any(not (65 <= byte <= 90 or 97 <= byte <= 122) for byte in kind):
            return False
        expected_crc = int.from_bytes(payload[data_end:end], "big")
        if zlib.crc32(payload[type_start:data_end]) & 0xFFFFFFFF != expected_crc:
            return False
        if chunk_index == 0 and kind != b"IHDR":
            return False
        if kind == b"IHDR":
            if saw_header or chunk_index != 0 or length != 13:
                return False
            width = int.from_bytes(payload[data_start : data_start + 4], "big")
            height = int.from_bytes(payload[data_start + 4 : data_start + 8], "big")
            if width < 1 or height < 1:
                return False
            saw_header = True
        elif not saw_header:
            return False
        if kind == b"IDAT":
            saw_data = True
        if kind == b"IEND":
            return length == 0 and saw_data and end == len(payload)
        offset = end
        chunk_index += 1
    return False


def _task5_request_plan(run_dir: Path, run: dict, manifest: dict, frame_index: int) -> dict:
    references = [{"role": "master", "path": "input/source/master.png"}]
    hashed_references = [
        {
            "role": "master",
            "path": "input/source/master.png",
            "sha256": manifest["source"]["normalized_sha256"],
        }
    ]
    if frame_index > 1:
        previous = next(
            (frame for frame in run["frames"] if frame["frame_index"] == frame_index - 1),
            None,
        )
        if not isinstance(previous, dict) or previous.get("status") != "ACCEPTED":
            raise ValueError("RUN_MANIFEST_INVALID")
        relative = f"normalized/frames/frame-{frame_index - 1:02d}.png"
        references.append({"role": "previous_accepted", "path": relative})
        hashed_references.append(
            {
                "role": "previous_accepted",
                "path": relative,
                "sha256": previous["normalized_sha256"],
            }
        )
    movement = manifest["movement_plan"][frame_index - 1]
    expected = {
        "frame_index": frame_index,
        "model": manifest["provider"]["model"],
        "references": references,
        "instructions": {
            "animation": manifest["request"]["animation"],
            "direction": manifest["request"]["direction"],
            "phase": movement["phase"],
            "body_motion": movement["body_motion"],
            "identity": "preserve the same character, face, proportions, clothing, equipment, palette and art style",
            "framing": "one complete character, bottom-center grounded, no crop",
            "background": "solid uniform run chroma, no shadow, no gradient",
        },
    }
    expected["request_fingerprint"] = _request_fingerprint(
        expected,
        run["artifacts"]["run_id"],
        run["manifest_sha256"],
        hashed_references,
    )
    plan_path = run_dir / f"input/requests/frame-{frame_index:02d}.json"
    plan_bytes = _read_single_link_bytes(plan_path, "RUN_MANIFEST_INVALID")
    plan = _decode_json(plan_bytes, "RUN_MANIFEST_INVALID")
    if plan != expected or plan_bytes != _json_bytes(expected):
        raise ValueError("RUN_MANIFEST_INVALID")
    return expected


def _validate_task5_request_record(
    record: object,
    position: int,
    run_dir: Path,
    run: dict,
    manifest: dict,
) -> None:
    _validate_request_record_content(record, position, run_dir, run, manifest, True)


def _validate_request_record_content(
    record: object,
    position: int,
    run_dir: Path,
    run: dict,
    manifest: dict,
    require_raw: bool,
) -> None:
    """Validate metadata; only pending-claim recovery may use verified finals instead of raw."""
    if not isinstance(record, dict) or not _TASK5_REQUEST_BASE_KEYS <= set(record):
        raise ValueError("RUN_MANIFEST_INVALID")
    if set(record) - _TASK5_REQUEST_BASE_KEYS - _TASK5_REQUEST_OPTIONAL_KEYS:
        raise ValueError("RUN_MANIFEST_INVALID")
    frame_index = record.get("frame_index")
    if (
        type(frame_index) is not int
        or frame_index != position + 1
        or frame_index > manifest["request"]["frame_count"]
        or record.get("attempt") != 1
        or record.get("model") != manifest["provider"]["model"]
        or not _is_sha256(record.get("request_fingerprint"))
        or record.get("status") not in _TASK5_KNOWN_STATUSES
    ):
        raise ValueError("RUN_MANIFEST_INVALID")
    submitted_at = record.get("submitted_at")
    timestamp = re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
        r"(?:\.[0-9]+)?(?:Z|[+-]([0-9]{2}):([0-9]{2}))",
        submitted_at,
    ) if isinstance(submitted_at, str) else None
    if timestamp is None or (
        timestamp[1] is not None
        and (int(timestamp[1]) > 23 or int(timestamp[2]) > 59)
    ):
        raise ValueError("RUN_MANIFEST_INVALID")
    try:
        parsed_at = datetime.fromisoformat(submitted_at.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        raise ValueError("RUN_MANIFEST_INVALID") from None
    if parsed_at.tzinfo is None:
        raise ValueError("RUN_MANIFEST_INVALID")
    try:
        estimated = Decimal(str(record.get("estimated_cost_usd")))
        authorized = Decimal(str(manifest["provider"]["pricing"]["effective_unit_price_usd"]))
    except (InvalidOperation, KeyError, TypeError, ValueError):
        raise ValueError("RUN_MANIFEST_INVALID") from None
    if not estimated.is_finite() or estimated != authorized:
        raise ValueError("RUN_MANIFEST_INVALID")
    request_id = record.get("request_id")
    if request_id is not None and not _task5_safe_request_id(request_id):
        raise ValueError("RUN_MANIFEST_INVALID")
    status = record["status"]
    if request_id is None and status not in {"SUBMITTING", "SUBMISSION_STATUS_UNKNOWN"}:
        raise ValueError("RUN_MANIFEST_INVALID")
    result_path = record.get("result_path")
    has_raw = "raw_sha256" in record
    has_safe_error = "safe_error_type" in record
    if result_path is None:
        if has_raw and (status not in {"COMPLETED", "REJECTED"} or not _is_sha256(record["raw_sha256"])):
            raise ValueError("RUN_MANIFEST_INVALID")
    else:
        expected_raw = f"raw/frame-{frame_index:02d}/attempt-01.png"
        if result_path != expected_raw or not _is_sha256(record.get("raw_sha256")):
            raise ValueError("RUN_MANIFEST_INVALID")
        if require_raw:
            raw = _read_single_link_bytes(run_dir / expected_raw, "RAW_PROVENANCE_MISMATCH")
            if hashlib.sha256(raw).hexdigest() != record["raw_sha256"]:
                raise ValueError("RAW_PROVENANCE_MISMATCH")
            if not _task5_structural_png(raw):
                raise ValueError("RUN_MANIFEST_INVALID")
    if has_safe_error and record["safe_error_type"] not in _TASK5_SAFE_ERROR_TYPES:
        raise ValueError("RUN_MANIFEST_INVALID")
    invalid = (
        (status == "SUBMITTING" and (request_id is not None or result_path is not None or has_raw or has_safe_error))
        or (status == "SUBMISSION_STATUS_UNKNOWN" and (request_id is not None or result_path is not None or has_raw))
        or (status in _TASK5_ACTIVE_STATUSES and (request_id is None or result_path is not None or has_raw or has_safe_error))
        or (status == "COMPLETED" and (request_id is None or has_safe_error))
        or (status in _TASK5_REMOTE_TERMINAL_STATUSES and (request_id is None or result_path is not None or has_raw))
        or (
            status == "REJECTED"
            and (
                request_id is None
                or result_path is not None
                or not has_safe_error
                or record["safe_error_type"] not in _TASK5_DETERMINISTIC_FAILURES
            )
        )
    )
    if invalid:
        raise ValueError("RUN_MANIFEST_INVALID")
    expected_plan = _task5_request_plan(run_dir, run, manifest, frame_index)
    if record["request_fingerprint"] != expected_plan["request_fingerprint"]:
        raise ValueError("RUN_MANIFEST_INVALID")


def _validate_recorded_evidence(
    run_dir: Path,
    frame: dict,
    request: dict,
) -> tuple[dict, bytes]:
    if set(frame) != _EVALUATION_FRAME_KEYS:
        raise ValueError("EVALUATION_EVIDENCE_INVALID")
    frame_index = frame["frame_index"]
    raw_relative = f"raw/frame-{frame_index:02d}/attempt-01.png"
    report_relative = f"reports/frames/frame-{frame_index:02d}.json"
    if (
        frame.get("raw_path") != raw_relative
        or frame.get("raw_sha256") != request.get("raw_sha256")
        or frame.get("report_path") != report_relative
        or not _is_sha256(frame.get("report_sha256"))
    ):
        raise ValueError("EVALUATION_EVIDENCE_INVALID")
    report_bytes = _read_single_link_bytes(
        run_dir / report_relative,
        "EVALUATION_EVIDENCE_INVALID",
    )
    if hashlib.sha256(report_bytes).hexdigest() != frame["report_sha256"]:
        raise ValueError("EVALUATION_EVIDENCE_INVALID")
    report = _decode_json(report_bytes, "EVALUATION_EVIDENCE_INVALID")
    provenance = report.get("provenance") if isinstance(report, dict) else None
    expected_provenance = {
        "raw_path": frame["raw_path"],
        "raw_sha256": frame["raw_sha256"],
        "normalized_path": frame["normalized_path"],
        "normalized_sha256": frame["normalized_sha256"],
    }
    if (
        not isinstance(report, dict)
        or set(report) != _EVALUATION_REPORT_KEYS
        or report_bytes != _json_bytes(report)
        or report.get("schema_version") != 1
        or report.get("frame_index") != frame_index
        or provenance != expected_provenance
        or not isinstance(report.get("normalization"), dict)
        or not isinstance(report.get("advisory"), dict)
    ):
        raise ValueError("EVALUATION_EVIDENCE_INVALID")
    if frame.get("status") == "ACCEPTED":
        normalized_relative = f"normalized/frames/frame-{frame_index:02d}.png"
        if (
            frame.get("normalized_path") != normalized_relative
            or not _is_sha256(frame.get("normalized_sha256"))
            or report.get("technical_result") != "PASS"
            or report.get("failures") != []
        ):
            raise ValueError("EVALUATION_EVIDENCE_INVALID")
        normalized = _read_single_link_bytes(
            run_dir / normalized_relative,
            "EVALUATION_EVIDENCE_INVALID",
        )
        if hashlib.sha256(normalized).hexdigest() != frame["normalized_sha256"]:
            raise ValueError("EVALUATION_EVIDENCE_INVALID")
    elif frame.get("status") == "REJECTED":
        if (
            frame.get("normalized_path") is not None
            or frame.get("normalized_sha256") is not None
            or report.get("technical_result") != "FAIL"
            or not isinstance(report.get("failures"), list)
            or len(report["failures"]) != 1
            or report["failures"][0] not in _EVALUATION_CONTENT_FAILURES
        ):
            raise ValueError("EVALUATION_EVIDENCE_INVALID")
    else:
        raise ValueError("EVALUATION_EVIDENCE_INVALID")
    return report, report_bytes


def _validate_evaluation_ledger(
    run_dir: Path,
    run: dict,
    manifest: dict,
    frame_index: int,
) -> tuple[dict[int, dict], dict[int, dict], dict | None]:
    if (
        set(run) != _EVALUATION_RUN_KEYS
        or run.get("schema_version") != manifest.get("schema_version")
        or not isinstance(run.get("requests"), list)
        or not isinstance(run.get("frames"), list)
        or not isinstance(run.get("artifacts"), dict)
        or set(run["artifacts"]) - {"run_id", "evaluation"}
    ):
        raise ValueError("RUN_MANIFEST_INVALID")
    frame_count = manifest["request"]["frame_count"]
    next_request = run.get("next_request")
    if type(frame_index) is not int or type(next_request) is not int or not 1 <= next_request <= frame_count + 1:
        raise ValueError("RUN_MANIFEST_INVALID")
    if run.get("state") == "AUTHORIZED":
        if run.get("terminal_reason") is not None:
            raise ValueError("RUN_MANIFEST_INVALID")
        expected_frame_indices = list(range(1, next_request))
        expected_statuses = ["ACCEPTED"] * len(expected_frame_indices)
    elif run.get("state") == "TECHNICAL_REJECTED":
        if run.get("terminal_reason") not in _EVALUATION_CONTENT_FAILURES:
            raise ValueError("RUN_MANIFEST_INVALID")
        expected_frame_indices = list(range(1, next_request + 1))
        expected_statuses = ["ACCEPTED"] * (len(expected_frame_indices) - 1) + ["REJECTED"]
    else:
        raise ValueError("RUN_NOT_AUTHORIZED")
    actual_indices = [frame.get("frame_index") if isinstance(frame, dict) else None for frame in run["frames"]]
    actual_statuses = [frame.get("status") if isinstance(frame, dict) else None for frame in run["frames"]]
    if actual_indices != expected_frame_indices or actual_statuses != expected_statuses:
        raise ValueError("RUN_MANIFEST_INVALID")
    frames = {frame["frame_index"]: frame for frame in run["frames"]}
    target_recorded = frame_index in frames
    if (
        (target_recorded and frame_index != expected_frame_indices[-1])
        or (not target_recorded and frame_index != next_request)
        or len(run["requests"]) != frame_index
    ):
        raise ValueError("FRAME_REQUEST_MISMATCH")
    requests: dict[int, dict] = {}
    for position, record in enumerate(run["requests"]):
        _validate_task5_request_record(record, position, run_dir, run, manifest)
        requests[position + 1] = record
    if any(
        record.get("status") != "COMPLETED" or record.get("result_path") is None
        for record in requests.values()
    ):
        raise ValueError("FRAME_REQUEST_MISMATCH")
    for index, frame in frames.items():
        _validate_recorded_evidence(run_dir, frame, requests[index])
    artifact = run["artifacts"].get("evaluation")
    if artifact is not None and (
        not isinstance(artifact, dict)
        or set(artifact) != _EVALUATION_CLAIM_KEYS
        or artifact.get("status") not in {"PENDING_PUBLICATION", "COMPLETED"}
    ):
        raise ValueError("EVALUATION_EVIDENCE_INVALID")
    if target_recorded:
        if artifact is None or artifact.get("frame_index") != frame_index or artifact.get("status") != "COMPLETED":
            raise ValueError("EVALUATION_EVIDENCE_INVALID")
    elif artifact is None:
        if frame_index != 1:
            raise ValueError("EVALUATION_EVIDENCE_INVALID")
    elif artifact.get("frame_index") == frame_index:
        if artifact.get("status") != "PENDING_PUBLICATION":
            raise ValueError("EVALUATION_EVIDENCE_INVALID")
    elif artifact.get("frame_index") != frame_index - 1 or artifact.get("status") != "COMPLETED":
        raise ValueError("EVALUATION_EVIDENCE_INVALID")
    if artifact is not None and artifact.get("frame_index") in frames:
        frame = frames[artifact["frame_index"]]
        report = _validate_recorded_evidence(run_dir, frame, requests[frame["frame_index"]])[0]
        expected_claim = _evaluation_claim(report, frame["report_path"], frame["report_sha256"])
        expected_claim["status"] = "COMPLETED"
        if artifact != expected_claim:
            raise ValueError("EVALUATION_EVIDENCE_INVALID")
    return requests, frames, artifact


def _png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGBA").save(buffer, format="PNG")
    return buffer.getvalue()


def _publish_new_bytes(path: Path, payload: bytes, exists_reason: str) -> _FileSnapshot:
    """Atomically publish new evidence without adopting or replacing a target."""
    path = Path(os.path.abspath(path))
    parent = _safe_chain(path.parent)
    with _PathGuard(parent, directory=True) as parent_guard:
        parent_guard.verify()
        if os.path.lexists(path):
            raise ValueError(exists_reason)
        temporary = parent / f".{path.name}.{uuid.uuid4().hex}.partial"
        try:
            with temporary.open("xb") as stream:
                _write_temp_payload(stream, payload, path)
                stream.flush()
                os.fsync(stream.fileno())
            temporary_bytes = _read_single_link_bytes(
                temporary,
                "ARTIFACT_IDENTITY_INVALID",
            )
            if temporary_bytes != payload:
                raise ValueError("ARTIFACT_IDENTITY_INVALID")
            snapshot = _file_snapshot(temporary, temporary_bytes)
            with _PathGuard(
                temporary,
                directory=False,
                writable_rename=True,
                expected_snapshot=snapshot,
            ) as temporary_guard:
                parent_guard.verify()
                temporary_guard.verify()
                if os.path.lexists(path):
                    raise ValueError(exists_reason)
                if _read_guard_bytes(temporary_guard, "PATH_IDENTITY_CHANGED") != payload:
                    raise ValueError("PATH_IDENTITY_CHANGED")
                try:
                    if os.name == "nt":
                        _rename_windows_handle(temporary_guard.handle, path, replace_if_exists=False)
                    elif sys.platform.startswith("linux"):
                        _publish_linux_no_replace(temporary_guard.path, path)
                    else:
                        raise ValueError("ATOMIC_PUBLISH_UNSUPPORTED")
                except FileExistsError:
                    raise ValueError(exists_reason) from None
                temporary_guard.path = path
                parent_guard.verify()
                temporary_guard.verify()
                published = _read_guard_bytes(temporary_guard, "PATH_IDENTITY_CHANGED")
                if published != payload:
                    raise ValueError("PATH_IDENTITY_CHANGED")
                if _file_snapshot(path, published).identity != snapshot.identity:
                    raise ValueError("PATH_IDENTITY_CHANGED")
        except FileExistsError:
            raise ValueError(exists_reason) from None
    return snapshot


def _read_png_image(path: Path, expected_sha256: str, reason: str) -> Image.Image:
    data = _read_single_link_bytes(path, reason)
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError(reason)
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as opened:
            if opened.format != "PNG":
                raise ValueError(reason)
            return opened.convert("RGBA").copy()
    except Exception:
        raise ValueError(reason) from None


def _evaluation_paths(run_dir: Path, frame_index: int) -> tuple[str, Path, str, Path]:
    normalized_relative = f"normalized/frames/frame-{frame_index:02d}.png"
    report_relative = f"reports/frames/frame-{frame_index:02d}.json"
    return (
        normalized_relative,
        run_dir / normalized_relative,
        report_relative,
        run_dir / report_relative,
    )


def _validate_evaluation_raw_path(
    run_dir: Path,
    frame_index: int,
    raw_path: Path,
) -> tuple[str, Path]:
    supplied = Path(raw_path)
    raw_relative = f"raw/frame-{frame_index:02d}/attempt-01.png"
    expected = run_dir / raw_relative
    if (
        ".." in supplied.parts
        or ":" in supplied.name
        or str(Path(os.path.abspath(supplied))) != str(Path(os.path.abspath(expected)))
        or supplied.name != "attempt-01.png"
    ):
        raise ValueError("FRAME_REQUEST_MISMATCH")
    try:
        _safe_chain(expected)
    except (OSError, ValueError):
        raise ValueError("WORKSPACE_CONFINEMENT_FAILED") from None
    return raw_relative, expected


def _request_for_evaluation(
    run_dir: Path,
    run_manifest_path: Path,
    run: dict,
    manifest: dict,
    frame_index: int,
) -> dict:
    if frame_index != run.get("next_request"):
        raise ValueError("FRAME_REQUEST_MISMATCH")
    frame_count = manifest.get("request", {}).get("frame_count")
    if type(frame_index) is not int or type(frame_count) is not int or not 1 <= frame_index <= frame_count:
        raise ValueError("FRAME_REQUEST_MISMATCH")
    requests = run.get("requests")
    if not isinstance(requests, list) or len(requests) != frame_index:
        raise ValueError("FRAME_REQUEST_MISMATCH")
    if any(
        not isinstance(item, dict) or item.get("frame_index") != index
        for index, item in enumerate(requests, 1)
    ):
        raise ValueError("FRAME_REQUEST_MISMATCH")
    record = requests[-1]
    raw_relative = f"raw/frame-{frame_index:02d}/attempt-01.png"
    if (
        record.get("status") != "COMPLETED"
        or record.get("result_path") != raw_relative
        or not isinstance(record.get("raw_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", record["raw_sha256"]) is None
    ):
        raise ValueError("FRAME_REQUEST_MISMATCH")
    plan_path = run_dir / f"input/requests/frame-{frame_index:02d}.json"
    request_plan = next_request_plan(run_manifest_path, plan_path)
    if (
        not isinstance(request_plan, dict)
        or record.get("request_fingerprint") != request_plan.get("request_fingerprint")
        or record.get("model") != request_plan.get("model")
    ):
        raise ValueError("FRAME_REQUEST_MISMATCH")
    return record


def _failed_evaluation_report(
    frame_index: int,
    reason: str,
    normalization: dict,
    raw_relative: str,
    raw_sha256: str,
    measured_advisory: dict | None = None,
) -> dict:
    advisory = {
        "alpha_bbox": None,
        "opaque_pixels": None,
        "visible_pixels": None,
        "measurement": {"status": "UNAVAILABLE", "reason": "NO_DECODED_ALPHA"},
        "metric_definitions": dict(_EVALUATION_METRIC_DEFINITIONS),
        "master": None,
        "previous": None,
    }
    if measured_advisory is not None:
        for key in ("alpha_bbox", "opaque_pixels", "visible_pixels", "measurement"):
            if key in measured_advisory:
                advisory[key] = measured_advisory[key]
    return {
        "schema_version": 1,
        "frame_index": frame_index,
        "technical_result": "FAIL",
        "failures": [reason],
        "normalization": normalization,
        "advisory": advisory,
        "provenance": {
            "raw_path": raw_relative,
            "raw_sha256": raw_sha256,
            "normalized_path": None,
            "normalized_sha256": None,
        },
    }


def _normalized_evaluation_report(
    run_dir: Path,
    run: dict,
    manifest: dict,
    frame_index: int,
    raw_relative: str,
    raw_sha256: str,
    normalized: Image.Image,
    normalization: dict,
) -> tuple[dict, bytes | None]:
    normalized_relative = f"normalized/frames/frame-{frame_index:02d}.png"
    master = _read_png_image(
        run_dir / manifest["source"]["normalized_path"],
        manifest["source"]["normalized_sha256"],
        "MASTER_HASH_MISMATCH",
    )
    previous = None
    if frame_index > 1:
        previous_frame = next(
            (
                item
                for item in run["frames"]
                if item.get("frame_index") == frame_index - 1
            ),
            None,
        )
        if not isinstance(previous_frame, dict) or previous_frame.get("status") != "ACCEPTED":
            raise ValueError("FRAME_REQUEST_MISMATCH")
        previous = _read_png_image(
            run_dir / previous_frame["normalized_path"],
            previous_frame["normalized_sha256"],
            "PREVIOUS_FRAME_HASH_MISMATCH",
        )
    report = technical_frame_report(
        normalized,
        master,
        previous,
        manifest,
        frame_index,
    )
    if report["technical_result"] != "PASS":
        raise ValueError("EVALUATION_INTERNAL_INVALID")
    normalized_bytes = _png_bytes(normalized)
    normalized_sha256 = hashlib.sha256(normalized_bytes).hexdigest()
    accepted_hashes = {
        item.get("normalized_sha256")
        for item in run["frames"]
        if (
            isinstance(item, dict)
            and item.get("status") == "ACCEPTED"
            and item.get("frame_index", frame_index) < frame_index
        )
    }
    if normalized_sha256 in accepted_hashes:
        report["technical_result"] = "FAIL"
        report["failures"] = ["FRAME_DUPLICATE"]
        normalized_bytes = None
        normalized_sha256 = None
        normalized_report_path = None
    else:
        normalized_report_path = normalized_relative
    report["normalization"] = normalization
    report["provenance"] = {
        "raw_path": raw_relative,
        "raw_sha256": raw_sha256,
        "normalized_path": normalized_report_path,
        "normalized_sha256": normalized_sha256,
    }
    return report, normalized_bytes


def _recompute_evaluation(
    run_dir: Path,
    run: dict,
    manifest: dict,
    frame_index: int,
    raw_relative: str,
    raw_sha256: str,
    raw_bytes: bytes,
) -> tuple[dict, bytes | None]:
    if hashlib.sha256(raw_bytes).hexdigest() != raw_sha256:
        raise ValueError("EVALUATION_EVIDENCE_INVALID")
    try:
        normalized, normalization = _normalize_generated_frame_bytes(
            raw_bytes,
            manifest,
        )
    except GenericImageBlocked as error:
        reason = str(error)
        if reason not in _EVALUATION_CONTENT_FAILURES:
            raise ValueError("EVALUATION_EVIDENCE_INVALID") from None
        evidence = error.evidence
        normalization = evidence.get(
            "normalization",
            {"status": "FAILED", "failure": reason},
        )
        return (
            _failed_evaluation_report(
                frame_index,
                reason,
                normalization,
                raw_relative,
                raw_sha256,
                evidence.get("advisory"),
            ),
            None,
        )
    return _normalized_evaluation_report(
        run_dir,
        run,
        manifest,
        frame_index,
        raw_relative,
        raw_sha256,
        normalized,
        normalization,
    )


def _revalidate_prior_evaluations(
    run_dir: Path,
    run: dict,
    manifest: dict,
    through_frame: int,
) -> None:
    """Rebuild every accepted prefix frame from one captured read of its raw."""
    if through_frame <= 0:
        return
    try:
        requests = run["requests"]
        frames = run["frames"]
        if not isinstance(requests, list) or not isinstance(frames, list):
            raise ValueError("EVALUATION_EVIDENCE_INVALID")
        frames_by_index = {
            frame["frame_index"]: frame
            for frame in frames
            if isinstance(frame, dict) and type(frame.get("frame_index")) is int
        }
        if len(frames_by_index) != len(frames):
            raise ValueError("EVALUATION_EVIDENCE_INVALID")
        for frame_index in range(1, through_frame + 1):
            if frame_index > len(requests):
                raise ValueError("EVALUATION_EVIDENCE_INVALID")
            request = requests[frame_index - 1]
            _validate_task5_request_record(
                request,
                frame_index - 1,
                run_dir,
                run,
                manifest,
            )
            frame = frames_by_index.get(frame_index)
            raw_relative = f"raw/frame-{frame_index:02d}/attempt-01.png"
            if (
                not isinstance(frame, dict)
                or frame.get("status") != "ACCEPTED"
                or request.get("status") != "COMPLETED"
                or request.get("result_path") != raw_relative
                or frame.get("raw_path") != raw_relative
                or frame.get("raw_sha256") != request.get("raw_sha256")
            ):
                raise ValueError("EVALUATION_EVIDENCE_INVALID")
            raw_bytes = _read_single_link_bytes(
                run_dir / raw_relative,
                "EVALUATION_EVIDENCE_INVALID",
            )
            raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
            if raw_sha256 != request.get("raw_sha256"):
                raise ValueError("EVALUATION_EVIDENCE_INVALID")
            report, report_bytes = _validate_recorded_evidence(
                run_dir,
                frame,
                request,
            )
            expected_report, expected_normalized = _recompute_evaluation(
                run_dir,
                run,
                manifest,
                frame_index,
                raw_relative,
                raw_sha256,
                raw_bytes,
            )
            if (
                expected_normalized is None
                or report != expected_report
                or report_bytes != _json_bytes(expected_report)
            ):
                raise ValueError("EVALUATION_EVIDENCE_INVALID")
            normalized = _read_single_link_bytes(
                run_dir / frame["normalized_path"],
                "EVALUATION_EVIDENCE_INVALID",
            )
            if normalized != expected_normalized:
                raise ValueError("EVALUATION_EVIDENCE_INVALID")

        artifact = run.get("artifacts", {}).get("evaluation")
        if not isinstance(artifact, dict):
            raise ValueError("EVALUATION_EVIDENCE_INVALID")
        artifact_frame = artifact.get("frame_index")
        if type(artifact_frame) is not int:
            raise ValueError("EVALUATION_EVIDENCE_INVALID")
        if artifact_frame <= through_frame:
            if artifact_frame != through_frame:
                raise ValueError("EVALUATION_EVIDENCE_INVALID")
            latest_frame = frames_by_index[through_frame]
            latest_report, _report_bytes = _validate_recorded_evidence(
                run_dir,
                latest_frame,
                requests[through_frame - 1],
            )
            expected_claim = _evaluation_claim(
                latest_report,
                latest_frame["report_path"],
                latest_frame["report_sha256"],
            )
            expected_claim["status"] = "COMPLETED"
            if artifact != expected_claim:
                raise ValueError("EVALUATION_EVIDENCE_INVALID")
    except (KeyError, OSError, TypeError, ValueError):
        raise ValueError("EVALUATION_EVIDENCE_INVALID") from None


def _validate_transport_evaluation_state(run_manifest_path: Path) -> None:
    """Read-only semantic validation used before the Node transport imports a client."""
    run_dir, _run_path, _run_bytes, run, _manifest_bytes, manifest = _read_run_pair(
        Path(run_manifest_path)
    )
    _validate_schema_pair(run, manifest)
    if run.get("manifest_sha256") != canonical_sha256(manifest):
        raise ValueError("MANIFEST_HASH_MISMATCH")
    _validate_persisted_inputs(run_dir, manifest)
    _validate_run_identity(run_dir, run)
    _validate_authorized_manifest(manifest)
    next_request = run.get("next_request")
    if type(next_request) is not int or next_request < 1:
        raise ValueError("RUN_MANIFEST_INVALID")
    _revalidate_prior_evaluations(run_dir, run, manifest, next_request - 1)
    current = next(
        (
            frame
            for frame in run.get("frames", [])
            if isinstance(frame, dict) and frame.get("frame_index") == next_request
        ),
        None,
    )
    requests = run.get("requests")
    if (
        isinstance(current, dict)
        and current.get("status") == "REJECTED"
        and isinstance(requests, list)
        and len(requests) >= next_request
        and isinstance(requests[next_request - 1], dict)
        and requests[next_request - 1].get("status") == "COMPLETED"
    ):
        request = requests[next_request - 1]
        raw_relative = f"raw/frame-{next_request:02d}/attempt-01.png"
        raw_bytes = _read_single_link_bytes(
            run_dir / raw_relative,
            "EVALUATION_EVIDENCE_INVALID",
        )
        _read_existing_evaluation(
            run_dir,
            run,
            manifest,
            next_request,
            raw_relative,
            hashlib.sha256(raw_bytes).hexdigest(),
            raw_bytes,
        )


def _evaluation_claim(
    report: dict,
    report_relative: str,
    report_sha256: str,
) -> dict:
    provenance = report["provenance"]
    return {
        "frame_index": report["frame_index"],
        "raw_path": provenance["raw_path"],
        "raw_sha256": provenance["raw_sha256"],
        "normalized_path": provenance["normalized_path"],
        "normalized_sha256": provenance["normalized_sha256"],
        "report_path": report_relative,
        "report_sha256": report_sha256,
        "technical_result": report["technical_result"],
        "failure": report["failures"][0] if report["failures"] else None,
        "status": "PENDING_PUBLICATION",
    }


def _read_existing_evaluation(
    run_dir: Path,
    run: dict,
    manifest: dict,
    frame_index: int,
    raw_relative: str,
    raw_sha256: str,
    raw_bytes: bytes,
) -> dict | None:
    requests, frames, artifact = _validate_evaluation_ledger(
        run_dir,
        run,
        manifest,
        frame_index,
    )
    frame = frames.get(frame_index)
    if frame is None:
        return None
    request = requests[frame_index]
    if (
        request.get("status") != "COMPLETED"
        or request.get("result_path") != raw_relative
        or request.get("raw_sha256") != raw_sha256
        or frame.get("raw_path") != raw_relative
        or frame.get("raw_sha256") != raw_sha256
        or hashlib.sha256(raw_bytes).hexdigest() != raw_sha256
    ):
        raise ValueError("RAW_PROVENANCE_MISMATCH")
    report, report_bytes = _validate_recorded_evidence(run_dir, frame, request)
    try:
        expected_report, expected_normalized = _recompute_evaluation(
            run_dir,
            run,
            manifest,
            frame_index,
            raw_relative,
            raw_sha256,
            raw_bytes,
        )
    except ValueError:
        raise ValueError("EVALUATION_EVIDENCE_INVALID")
    if report != expected_report or report_bytes != _json_bytes(expected_report):
        raise ValueError("EVALUATION_EVIDENCE_INVALID")
    if frame.get("status") == "ACCEPTED":
        normalized = _read_single_link_bytes(
            run_dir / frame["normalized_path"],
            "EVALUATION_EVIDENCE_INVALID",
        )
        if expected_normalized is None or normalized != expected_normalized:
            raise ValueError("EVALUATION_EVIDENCE_INVALID")
        if (
            run.get("state") != "AUTHORIZED"
            or run.get("terminal_reason") is not None
            or run.get("next_request") != frame_index + 1
        ):
            raise ValueError("EVALUATION_EVIDENCE_INVALID")
    elif (
        expected_normalized is not None
        or run.get("state") != "TECHNICAL_REJECTED"
        or run.get("terminal_reason") != report["failures"][0]
        or run.get("next_request") != frame_index
    ):
        raise ValueError("EVALUATION_EVIDENCE_INVALID")
    expected_claim = _evaluation_claim(report, frame["report_path"], frame["report_sha256"])
    expected_claim["status"] = "COMPLETED"
    if artifact != expected_claim:
        raise ValueError("EVALUATION_EVIDENCE_INVALID")
    return report


def _publish_or_recover_claimed(
    path: Path,
    payload: bytes,
    claim_was_present: bool,
) -> None:
    if os.path.lexists(path):
        if not claim_was_present:
            raise ValueError("EVALUATION_EVIDENCE_EXISTS")
        try:
            existing = _read_single_link_bytes(path, "EVALUATION_EVIDENCE_INVALID")
        except (OSError, ValueError):
            raise ValueError("EVALUATION_EVIDENCE_INVALID") from None
        if existing != payload:
            raise ValueError("EVALUATION_EVIDENCE_INVALID")
        return
    _publish_new_bytes(path, payload, "EVALUATION_EVIDENCE_EXISTS")


def record_evaluation(
    run_manifest_path: Path,
    frame_index: int,
    raw_path: Path,
) -> dict:
    """Evaluate one completed transport result and durably advance or reject."""
    run_manifest_path = Path(run_manifest_path)
    run_dir, run_path, run_bytes, run, _manifest_bytes, manifest = _read_run_pair(
        run_manifest_path
    )
    _validate_schema_pair(run, manifest)
    if run.get("manifest_sha256") != canonical_sha256(manifest):
        raise ValueError("MANIFEST_HASH_MISMATCH")
    _validate_persisted_inputs(run_dir, manifest)
    if run.get("state") not in ("AUTHORIZED", "TECHNICAL_REJECTED"):
        raise ValueError("RUN_NOT_AUTHORIZED")
    _validate_run_identity(run_dir, run)
    _validate_authorized_manifest(manifest)
    try:
        requests, _frames, _evaluation_artifact = _validate_evaluation_ledger(
            run_dir,
            run,
            manifest,
            frame_index,
        )
    except ValueError as error:
        if str(error) in {"LINKED_PATH", "REPARSE_PATH", "PATH_IDENTITY_CHANGED"}:
            raise ValueError("WORKSPACE_CONFINEMENT_FAILED") from None
        raise
    _revalidate_prior_evaluations(
        run_dir,
        run,
        manifest,
        frame_index - 1,
    )
    raw_relative, raw_absolute = _validate_evaluation_raw_path(run_dir, frame_index, raw_path)
    try:
        raw_bytes = _read_single_link_bytes(raw_absolute, "RAW_PROVENANCE_MISMATCH")
        raw_snapshot = _file_snapshot(raw_absolute, raw_bytes)
    except (OSError, ValueError):
        raise ValueError("RAW_PROVENANCE_MISMATCH") from None
    raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()

    existing = _read_existing_evaluation(
        run_dir,
        run,
        manifest,
        frame_index,
        raw_relative,
        raw_sha256,
        raw_bytes,
    )
    if existing is not None:
        return existing
    if run.get("state") == "TECHNICAL_REJECTED" or run.get("terminal_reason") is not None:
        raise ValueError("FRAME_REQUEST_MISMATCH")
    request = requests[frame_index]
    if raw_sha256 != request["raw_sha256"]:
        raise ValueError("RAW_PROVENANCE_MISMATCH")

    normalized_relative, normalized_path, report_relative, report_path = _evaluation_paths(
        run_dir,
        frame_index,
    )
    evaluation_artifact = run.get("artifacts", {}).get("evaluation")
    claim_was_present = (
        isinstance(evaluation_artifact, dict)
        and evaluation_artifact.get("frame_index") == frame_index
    )
    if not claim_was_present and (os.path.lexists(normalized_path) or os.path.lexists(report_path)):
        raise ValueError("EVALUATION_EVIDENCE_EXISTS")

    raw_root = run_dir / "raw"
    raw_parent = raw_absolute.parent
    try:
        with _PathGuard(run_dir, directory=True) as run_guard:
            with _PathGuard(raw_root, directory=True) as raw_root_guard:
                with _PathGuard(raw_parent, directory=True) as raw_parent_guard:
                    with _PathGuard(
                        raw_absolute,
                        directory=False,
                        expected_snapshot=raw_snapshot,
                    ) as raw_guard:
                        run_guard.verify()
                        raw_root_guard.verify()
                        raw_parent_guard.verify()
                        raw_guard.verify()
                        guarded_raw_bytes = _read_guard_bytes(
                            raw_guard,
                            "RAW_PROVENANCE_MISMATCH",
                        )
                        if guarded_raw_bytes != raw_bytes:
                            raise ValueError("RAW_PROVENANCE_MISMATCH")
                        try:
                            normalized, normalization = _normalize_generated_frame_bytes(
                                guarded_raw_bytes,
                                manifest,
                            )
                            content_failure = None
                        except GenericImageBlocked as error:
                            content_failure = str(error)
                            if content_failure not in _EVALUATION_CONTENT_FAILURES:
                                raise ValueError(content_failure) from None
                            normalized = None
                            failure_evidence = error.evidence
                            normalization = failure_evidence.get(
                                "normalization",
                                {"status": "FAILED", "failure": content_failure},
                            )
                            measured_advisory = failure_evidence.get("advisory")
                        run_guard.verify()
                        raw_root_guard.verify()
                        raw_parent_guard.verify()
                        raw_guard.verify()
                        if _read_guard_bytes(raw_guard, "RAW_PROVENANCE_MISMATCH") != raw_bytes:
                            raise ValueError("RAW_PROVENANCE_MISMATCH")
    except GenericImageBlocked:
        raise
    except ValueError as error:
        if str(error) in _EVALUATION_CONTENT_FAILURES or str(error) == "RAW_PROVENANCE_MISMATCH":
            raise
        if str(error) in {"LINKED_PATH", "REPARSE_PATH", "PATH_IDENTITY_CHANGED"}:
            raise ValueError("WORKSPACE_CONFINEMENT_FAILED") from None
        raise

    if normalized is None:
        report = _failed_evaluation_report(
            frame_index,
            content_failure,
            normalization,
            raw_relative,
            raw_sha256,
            measured_advisory,
        )
        normalized_bytes = None
    else:
        report, normalized_bytes = _normalized_evaluation_report(
            run_dir,
            run,
            manifest,
            frame_index,
            raw_relative,
            raw_sha256,
            normalized,
            normalization,
        )

    report_bytes = _json_bytes(report)
    report_sha256 = hashlib.sha256(report_bytes).hexdigest()
    claim = _evaluation_claim(report, report_relative, report_sha256)
    if claim_was_present:
        expected_claim = dict(claim)
        expected_claim["status"] = evaluation_artifact.get("status")
        if evaluation_artifact != expected_claim or evaluation_artifact.get("status") not in {
            "PENDING_PUBLICATION",
            "COMPLETED",
        }:
            raise ValueError("EVALUATION_EVIDENCE_INVALID")
    else:
        claimed_run = copy.deepcopy(run)
        claimed_run["artifacts"]["evaluation"] = claim
        _rewrite_existing_json(run_path, claimed_run, run_bytes)
        run = claimed_run
        run_bytes = _json_bytes(claimed_run)

    if normalized_bytes is not None:
        _publish_or_recover_claimed(
            normalized_path,
            normalized_bytes,
            claim_was_present,
        )
    _publish_or_recover_claimed(
        report_path,
        report_bytes,
        claim_was_present,
    )

    final_run = copy.deepcopy(run)
    provenance = report["provenance"]
    final_run["frames"].append(
        {
            "frame_index": frame_index,
            "status": "ACCEPTED" if report["technical_result"] == "PASS" else "REJECTED",
            "raw_path": raw_relative,
            "raw_sha256": raw_sha256,
            "normalized_path": provenance["normalized_path"],
            "normalized_sha256": provenance["normalized_sha256"],
            "report_path": report_relative,
            "report_sha256": report_sha256,
        }
    )
    final_run["artifacts"]["evaluation"]["status"] = "COMPLETED"
    if report["technical_result"] == "PASS":
        final_run["next_request"] = frame_index + 1
    else:
        final_run["state"] = "TECHNICAL_REJECTED"
        final_run["terminal_reason"] = report["failures"][0]
    _rewrite_existing_json(run_path, final_run, run_bytes)
    return report


_ASSEMBLY_DURATION_MS = 100
_ASSEMBLY_FINAL_PATHS = (
    "normalized/sprite-sheet.png",
    "previews/loop.gif",
    "previews/contact-sheet.png",
    "reports/qa-report.json",
    "reports/provenance-report.json",
)


def _stable_request_content(
    run_dir: Path,
    run: dict,
    manifest: dict,
    frame_index: int,
) -> tuple[str, list[dict]]:
    plan = _task5_request_plan(run_dir, run, manifest, frame_index)
    request = copy.deepcopy(plan)
    request.pop("request_fingerprint")
    references = [
        {
            "role": "master",
            "path": "input/source/master.png",
            "sha256": manifest["source"]["normalized_sha256"],
        }
    ]
    if frame_index > 1:
        previous = run["frames"][frame_index - 2]
        references.append(
            {
                "role": "previous_accepted",
                "path": previous["normalized_path"],
                "sha256": previous["normalized_sha256"],
            }
        )
    return (
        canonical_sha256(
            {
                "schema_version": 1,
                "request": request,
                "references": references,
            }
        ),
        references,
    )


def _assembly_context(
    run_manifest_path: Path,
    recovery_mode: str | None = None,
) -> tuple[Path, Path, bytes, dict, dict, tuple[Image.Image, ...], list[dict]]:
    if recovery_mode not in (None, "evidence", "finals"):
        raise ValueError("ASSEMBLY_RECOVERY_REQUIRED")
    run_dir, run_path, run_bytes, run, _manifest_bytes, manifest = _read_run_pair(
        Path(run_manifest_path)
    )
    _validate_schema_pair(run, manifest)
    if set(run) != _EVALUATION_RUN_KEYS:
        raise ValueError("RUN_MANIFEST_INVALID")
    if run.get("manifest_sha256") != canonical_sha256(manifest):
        raise ValueError("MANIFEST_HASH_MISMATCH")
    _validate_persisted_inputs(run_dir, manifest)
    _validate_authorized_manifest(manifest)
    _validate_run_identity(run_dir, run)
    if run.get("state") not in {"AUTHORIZED", "READY_FOR_VISUAL_REVIEW"}:
        raise ValueError("ASSEMBLY_NOT_READY")
    if run.get("terminal_reason") is not None:
        raise ValueError("ASSEMBLY_NOT_READY")
    artifacts = run.get("artifacts")
    if (
        not isinstance(artifacts, dict)
        or set(artifacts) - {"run_id", "evaluation", "assembly"}
    ):
        raise ValueError("RUN_MANIFEST_INVALID")
    assembly = artifacts.get("assembly")
    if recovery_mode is not None:
        # Recovery derives its claim from independent data, never from this field.
        pass
    elif run["state"] == "READY_FOR_VISUAL_REVIEW":
        if not isinstance(assembly, dict) or assembly.get("status") != "COMPLETED":
            raise ValueError("FINAL_ARTIFACT_INVALID")
    elif isinstance(assembly, dict) and assembly.get("status") == "COMPLETED":
        raise ValueError("FINAL_ARTIFACT_INVALID")

    try:
        frame_count = manifest["request"]["frame_count"]
        authorization = manifest["authorization"]
        pricing = manifest["provider"]["pricing"]
        effective_price = Decimal(str(pricing["effective_unit_price_usd"]))
        worst_case = Decimal(str(authorization["worst_case_usd"]))
        authorized_budget = Decimal(str(authorization["authorized_budget_usd"]))
    except (InvalidOperation, KeyError, TypeError, ValueError):
        raise ValueError("MANIFEST_INVALID") from None
    requests = run.get("requests")
    frame_records = run.get("frames")
    if (
        type(frame_count) is not int
        or frame_count <= 0
        or run.get("next_request") != frame_count + 1
        or authorization.get("planned_requests") != frame_count
        or not isinstance(requests, list)
        or len(requests) != frame_count
        or any(
            not isinstance(request, dict)
            or type(request.get("attempt")) is not int
            or request["attempt"] != 1
            for request in requests
        )
        or not isinstance(frame_records, list)
        or len(frame_records) != frame_count
        or not effective_price.is_finite()
        or not worst_case.is_finite()
        or not authorized_budget.is_finite()
    ):
        raise ValueError("ASSEMBLY_NOT_READY")

    validation_run = copy.deepcopy(run)
    validation_run["state"] = "AUTHORIZED"
    validation_run["artifacts"].pop("assembly", None)
    if recovery_mode == "finals":
        # This branch cannot publish or return READY. Its caller must verify the
        # complete existing outputs against recomputed bytes before creating a
        # pending claim. Normal assembly still revalidates every raw evaluation.
        latest_report = None
        for index, (frame, request) in enumerate(zip(frame_records, requests)):
            _validate_request_record_content(request, index, run_dir, validation_run, manifest, False)
            latest_report, _report_bytes = _validate_recorded_evidence(run_dir, frame, request)
        latest_frame = frame_records[-1]
        expected_evaluation = _evaluation_claim(
            latest_report, latest_frame["report_path"], latest_frame["report_sha256"]
        )
        expected_evaluation["status"] = "COMPLETED"
        if validation_run["artifacts"].get("evaluation") != expected_evaluation:
            raise ValueError("EVALUATION_EVIDENCE_INVALID")
    else:
        _revalidate_prior_evaluations(run_dir, validation_run, manifest, frame_count)
    expected_indices = list(range(1, frame_count + 1))
    if (
        [frame.get("frame_index") for frame in run["frames"]] != expected_indices
        or any(frame.get("status") != "ACCEPTED" for frame in run["frames"])
        or [request.get("frame_index") for request in run["requests"]] != expected_indices
        or any(request.get("status") != "COMPLETED" for request in run["requests"])
    ):
        raise ValueError("ASSEMBLY_NOT_READY")
    try:
        request_costs = tuple(
            Decimal(str(request["estimated_cost_usd"])) for request in run["requests"]
        )
        recorded_cost = (
            _sum_decimals_exact(request_costs)
            if manifest["schema_version"] >= 2
            else sum(request_costs, Decimal("0"))
        )
    except (InvalidOperation, KeyError, TypeError, ValueError):
        raise ValueError("RUN_MANIFEST_INVALID") from None
    if (
        recorded_cost != (
            _multiply_decimal_exact(effective_price, Decimal(frame_count))
            if manifest["schema_version"] >= 2
            else effective_price * frame_count
        )
        or recorded_cost != worst_case
        or recorded_cost > authorized_budget
    ):
        raise ValueError("ASSEMBLY_COST_INVALID")

    hashes = [frame["normalized_sha256"] for frame in run["frames"]]
    if len(set(hashes)) != frame_count:
        raise ValueError("ASSEMBLY_DUPLICATE_FRAME")
    width = manifest["output"]["width"]
    height = manifest["output"]["height"]
    frames = []
    request_provenance = []
    for frame_index, (frame, request) in enumerate(
        zip(run["frames"], run["requests"]),
        1,
    ):
        image = _read_png_image(
            run_dir / frame["normalized_path"],
            frame["normalized_sha256"],
            "EVALUATION_EVIDENCE_INVALID",
        )
        if image.size != (width, height):
            raise ValueError("EVALUATION_EVIDENCE_INVALID")
        frames.append(image)
        request_content_sha256, references = _stable_request_content(
            run_dir,
            validation_run,
            manifest,
            frame_index,
        )
        request_provenance.append(
            {
                "frame_index": frame_index,
                "attempt": request["attempt"],
                "model": request["model"],
                "request_content_sha256": request_content_sha256,
                "references": references,
                "request_id": request["request_id"],
                "status": request["status"],
                "estimated_cost_usd": request["estimated_cost_usd"],
                "result_path": request["result_path"],
                "raw_sha256": request["raw_sha256"],
            }
        )
    return (
        run_dir,
        run_path,
        run_bytes,
        run,
        manifest,
        tuple(frames),
        request_provenance,
    )


def _assembly_payloads(
    run: dict,
    manifest: dict,
    frames: tuple[Image.Image, ...],
    request_provenance: list[dict],
) -> tuple[dict, dict, dict[str, bytes]]:
    duration_ms = _ASSEMBLY_DURATION_MS
    width = manifest["output"]["width"]
    height = manifest["output"]["height"]
    sheet_bytes = _png_bytes(assemble_sheet(frames))
    loop_bytes = _loop_gif_bytes(frames, duration_ms)
    contact_bytes = _png_bytes(assemble_sheet(frames))
    try:
        with Image.open(io.BytesIO(loop_bytes)) as loop:
            if loop.n_frames != len(frames) or loop.size != (width, height):
                raise ValueError("GIF_SEQUENCE_INVALID")
    except OSError:
        raise ValueError("GIF_SEQUENCE_INVALID") from None

    frame_artifacts = [
        {
            "frame_index": frame["frame_index"],
            "path": frame["normalized_path"],
            "sha256": frame["normalized_sha256"],
            "dimensions": [width, height],
            "mode": "RGBA",
        }
        for frame in run["frames"]
    ]
    assembled_artifacts = {
        "sprite_sheet": {
            "path": "normalized/sprite-sheet.png",
            "sha256": hashlib.sha256(sheet_bytes).hexdigest(),
            "dimensions": [width * len(frames), height],
            "mode": "RGBA",
        },
        "loop_gif": {
            "path": "previews/loop.gif",
            "sha256": hashlib.sha256(loop_bytes).hexdigest(),
            "dimensions": [width, height],
            "frame_count": len(frames),
            "duration_ms": duration_ms,
        },
        "contact_sheet": {
            "path": "previews/contact-sheet.png",
            "sha256": hashlib.sha256(contact_bytes).hexdigest(),
            "dimensions": [width * len(frames), height],
            "mode": "RGBA",
        },
    }
    qa = {
        "schema_version": 1,
        "state": "READY_FOR_VISUAL_REVIEW",
        "technical_result": "PASS",
        "failures": [],
        "machine_ceiling": "READY_FOR_VISUAL_REVIEW",
        "human_review_required": True,
        "loop_review_required": True,
        "frame_count": len(frames),
        "frame_dimensions": [width, height],
        "checks": {
            "complete_accepted_chain": True,
            "frame_order": True,
            "distinct_normalized_frames": True,
            "frame_and_report_hashes": True,
            "request_content_hashes": True,
            "request_count_within_authorization": True,
            "recorded_cost_within_authorization": True,
        },
        "artifacts": {
            "frames": frame_artifacts,
            **assembled_artifacts,
        },
    }
    source = manifest["source"]
    authorization = manifest["authorization"]
    provenance = {
        "schema_version": 1,
        "source": {
            "original_filename": source["original_filename"],
            "original_path": source["original_path"],
            "original_sha256": source["sha256"],
            "normalized_path": source["normalized_path"],
            "normalized_sha256": source["normalized_sha256"],
        },
        "request": copy.deepcopy(manifest["request"]),
        "output": copy.deepcopy(manifest["output"]),
        "movement_plan_sha256": canonical_sha256(manifest["movement_plan"]),
        "provider": copy.deepcopy(manifest["provider"]),
        "authorization": {
            "planned_requests": authorization["planned_requests"],
            "worst_case_usd": authorization["worst_case_usd"],
            "max_budget_usd": authorization["max_budget_usd"],
            "authorized_budget_usd": authorization["authorized_budget_usd"],
            "contract_sha256": authorization["contract_sha256"],
        },
        "requests": request_provenance,
        "frames": frame_artifacts,
        "artifacts": assembled_artifacts,
    }
    payloads = {
        "normalized/sprite-sheet.png": sheet_bytes,
        "previews/loop.gif": loop_bytes,
        "previews/contact-sheet.png": contact_bytes,
        "reports/qa-report.json": _json_bytes(qa),
        "reports/provenance-report.json": _json_bytes(provenance),
    }
    return qa, provenance, payloads


def _assembly_claim(payloads: dict[str, bytes], status: str) -> dict:
    return {
        "status": status,
        "duration_ms": _ASSEMBLY_DURATION_MS,
        "files": [
            {
                "path": relative,
                "sha256": hashlib.sha256(payloads[relative]).hexdigest(),
            }
            for relative in _ASSEMBLY_FINAL_PATHS
        ],
    }


def _capture_exact_file(
    path: Path,
    expected: bytes,
    reason: str,
) -> tuple[Path, bytes, _FileSnapshot]:
    guard = None
    try:
        guard = _PathGuard(path, directory=False)
        current = _read_guard_bytes(guard, reason)
        if current != expected:
            raise ValueError(reason)
        snapshot = _file_snapshot(path, current)
        if not _identities_match(guard.identity, snapshot.identity):
            raise ValueError(reason)
        guard.verify()
        opened_after = _read_guard_bytes(guard, reason)
        named_after = _read_single_link_bytes(path, reason)
        named_snapshot = _file_snapshot(path, named_after)
        if (
            opened_after != expected
            or named_after != expected
            or not _snapshots_match(snapshot, named_snapshot)
        ):
            raise ValueError(reason)
    except (OSError, ValueError):
        raise ValueError(reason) from None
    finally:
        if guard is not None:
            guard.close()
    return path, expected, named_snapshot


def _capture_assembly_evidence(
    run_dir: Path,
    run: dict,
    manifest: dict,
    include_raw: bool = True,
) -> tuple[tuple[Path, bytes, _FileSnapshot], ...]:
    relatives = {
        "reports/manifest.json",
        "input/movement-plan.json",
        manifest["source"]["original_path"],
        manifest["source"]["normalized_path"],
    }
    for frame_index, frame in enumerate(run["frames"], 1):
        relatives.update(
            {
                f"input/requests/frame-{frame_index:02d}.json",
                frame["normalized_path"],
                frame["report_path"],
            }
        )
        if include_raw:
            relatives.add(frame["raw_path"])
    captured = []
    for relative in sorted(relatives):
        path = run_dir / relative
        try:
            expected = _read_single_link_bytes(path, "ASSEMBLY_EVIDENCE_INVALID")
        except (OSError, ValueError):
            raise ValueError("ASSEMBLY_EVIDENCE_INVALID") from None
        captured.append(
            _capture_exact_file(path, expected, "ASSEMBLY_EVIDENCE_INVALID")
        )
    return tuple(captured)


def _verify_captured_files(
    captured: tuple[tuple[Path, bytes, _FileSnapshot], ...],
    reason: str,
) -> None:
    try:
        for path, expected, snapshot in captured:
            current = _read_single_link_bytes(path, reason)
            if current != expected or _file_snapshot(path, current) != snapshot:
                raise ValueError(reason)
    except (OSError, ValueError):
        raise ValueError(reason) from None


def _verify_lifecycle_bytes(path: Path, expected: bytes) -> None:
    try:
        current = _read_single_link_bytes(path, "ASSEMBLY_CLAIM_INVALID")
    except (OSError, ValueError):
        raise ValueError("ASSEMBLY_CLAIM_INVALID") from None
    if current != expected:
        raise ValueError("ASSEMBLY_CLAIM_INVALID")


def _restore_pending_assembly(
    run_path: Path,
    pending_run: dict,
    ready_bytes: bytes,
) -> None:
    pending_bytes = _json_bytes(pending_run)
    try:
        current = _read_single_link_bytes(run_path, "ASSEMBLY_CLAIM_INVALID")
        if current == pending_bytes:
            return
        if current != ready_bytes:
            raise ValueError("ASSEMBLY_CLAIM_INVALID")
        _rewrite_existing_json(run_path, pending_run, ready_bytes)
        _verify_lifecycle_bytes(run_path, pending_bytes)
    except (OSError, ValueError):
        raise ValueError("ASSEMBLY_CLAIM_INVALID") from None


def _publish_or_recover_assembly(
    path: Path,
    payload: bytes,
    allow_adoption: bool,
) -> _FileSnapshot:
    if os.path.lexists(path):
        if not allow_adoption:
            raise ValueError("FINAL_ARTIFACT_EXISTS")
        try:
            existing = _read_single_link_bytes(path, "FINAL_ARTIFACT_INVALID")
            snapshot = _file_snapshot(path, existing)
        except (OSError, ValueError):
            raise ValueError("FINAL_ARTIFACT_INVALID") from None
        if existing != payload:
            raise ValueError("FINAL_ARTIFACT_INVALID")
        return snapshot
    return _publish_new_bytes(path, payload, "FINAL_ARTIFACT_EXISTS")


def _identities_match(expected: _Identity, current: _Identity) -> bool:
    stable_attributes = ~0x10000000
    return (
        expected.device == current.device
        and expected.inode == current.inode
        and expected.file_type == current.file_type
        and not (
            (expected.attributes ^ current.attributes) & stable_attributes
        )
    )


def _snapshots_match(expected: _FileSnapshot, current: _FileSnapshot) -> bool:
    return (
        _identities_match(expected.identity, current.identity)
        and expected.size == current.size
        and expected.modified_ns == current.modified_ns
        and expected.digest == current.digest
    )


def _hold_exact_captured_file(
    path: Path,
    payload: bytes,
    published_snapshot: _FileSnapshot,
    reason: str,
) -> _PathGuard:
    guard = None
    try:
        named = _read_single_link_bytes(path, reason)
        named_snapshot = _file_snapshot(path, named)
        if named != payload or not _snapshots_match(published_snapshot, named_snapshot):
            raise ValueError(reason)
        guard = _PathGuard(
            path,
            directory=False,
            expected_snapshot=published_snapshot,
        )
        if not _snapshots_match(
            published_snapshot,
            _file_snapshot(path, named),
        ):
            raise ValueError(reason)
        guard.verify()
        if _read_guard_bytes(guard, reason) != payload:
            raise ValueError(reason)
        return guard
    except (OSError, ValueError):
        if guard is not None:
            guard.close()
        raise ValueError(reason) from None


def _verify_guarded_files(
    guarded: list[tuple[_PathGuard, bytes, _FileSnapshot]],
    reason: str,
) -> None:
    try:
        for guard, payload, published_snapshot in guarded:
            guard.verify()
            opened = _read_guard_bytes(guard, reason)
            named = _read_single_link_bytes(guard.path, reason)
            named_snapshot = _file_snapshot(guard.path, named)
            if (
                opened != payload
                or named != payload
                or not _snapshots_match(published_snapshot, named_snapshot)
            ):
                raise ValueError(reason)
    except (OSError, ValueError):
        raise ValueError(reason) from None


def _hold_exact_assembly_final(
    path: Path,
    payload: bytes,
    published_snapshot: _FileSnapshot,
) -> _PathGuard:
    return _hold_exact_captured_file(
        path,
        payload,
        published_snapshot,
        "FINAL_ARTIFACT_INVALID",
    )


def _verify_guarded_assembly_finals(
    guarded: list[tuple[_PathGuard, bytes, _FileSnapshot]],
) -> None:
    _verify_guarded_files(guarded, "FINAL_ARTIFACT_INVALID")


def _verified_ready_assembly_result(
    run_path: Path,
    ready_run_bytes: bytes,
    expected_ready_run: dict,
    lifecycle_guard: _PathGuard,
    guarded_evidence: list[tuple[_PathGuard, bytes, _FileSnapshot]],
    guarded_finals: list[tuple[_PathGuard, bytes, _FileSnapshot]],
    expected_qa: dict,
) -> dict:
    """Construct the return value while every authoritative guard is live."""
    _verify_lifecycle_bytes(run_path, ready_run_bytes)
    _verify_guarded_files(guarded_evidence, "ASSEMBLY_EVIDENCE_INVALID")
    _verify_guarded_assembly_finals(guarded_finals)
    lifecycle_guard.verify()
    opened_run = _read_guard_bytes(lifecycle_guard, "ASSEMBLY_CLAIM_INVALID")
    named_run = _read_single_link_bytes(run_path, "ASSEMBLY_CLAIM_INVALID")
    if opened_run != ready_run_bytes or named_run != ready_run_bytes:
        raise ValueError("ASSEMBLY_CLAIM_INVALID")
    persisted_run = _decode_json(opened_run, "ASSEMBLY_CLAIM_INVALID")
    if persisted_run != expected_ready_run:
        raise ValueError("ASSEMBLY_CLAIM_INVALID")
    qa_guard = next(
        (
            guard
            for guard, _payload, _snapshot in guarded_finals
            if guard.path.name == "qa-report.json"
        ),
        None,
    )
    if qa_guard is None:
        raise ValueError("FINAL_ARTIFACT_INVALID")
    persisted_qa = _decode_json(
        _read_guard_bytes(qa_guard, "FINAL_ARTIFACT_INVALID"),
        "FINAL_ARTIFACT_INVALID",
    )
    if persisted_qa != expected_qa:
        raise ValueError("FINAL_ARTIFACT_INVALID")
    _verify_guarded_files(guarded_evidence, "ASSEMBLY_EVIDENCE_INVALID")
    _verify_guarded_assembly_finals(guarded_finals)
    lifecycle_guard.verify()
    if _read_guard_bytes(lifecycle_guard, "ASSEMBLY_CLAIM_INVALID") != ready_run_bytes:
        raise ValueError("ASSEMBLY_CLAIM_INVALID")
    return persisted_qa


def _canonical_ready_recovery(
    run_path: Path,
    entry_bytes: bytes,
) -> tuple[dict, list[tuple[_PathGuard, bytes, _FileSnapshot]]]:
    """Derive pending hashes from guarded evidence, or complete verified outputs."""
    for mode in ("evidence", "finals"):
        guarded = []
        try:
            context = _assembly_context(run_path, mode)
            if context[2] != entry_bytes:
                raise ValueError("ASSEMBLY_CLAIM_INVALID")
            run_dir, _path, _bytes, run, manifest, _frames, _requests = context
            captured = _capture_assembly_evidence(run_dir, run, manifest, mode == "evidence")
            for path, payload, snapshot in captured:
                guard = _hold_exact_captured_file(path, payload, snapshot, "ASSEMBLY_EVIDENCE_INVALID")
                guarded.append((guard, payload, snapshot))
            # The authoritative semantic validation follows capture and runs
            # while all identities and bytes used to derive the claim are held.
            context = _assembly_context(run_path, mode)
            if context[2] != entry_bytes:
                raise ValueError("ASSEMBLY_CLAIM_INVALID")
            _qa, _provenance, payloads = _assembly_payloads(*context[3:7])
            if mode == "finals":
                for relative in _ASSEMBLY_FINAL_PATHS:
                    path, payload, snapshot = _capture_exact_file(
                        run_dir / relative, payloads[relative], "FINAL_ARTIFACT_INVALID"
                    )
                    guard = _hold_exact_captured_file(path, payload, snapshot, "FINAL_ARTIFACT_INVALID")
                    guarded.append((guard, payload, snapshot))
            _verify_guarded_files(guarded, "ASSEMBLY_EVIDENCE_INVALID")
            _verify_lifecycle_bytes(run_path, entry_bytes)
            pending = copy.deepcopy(context[3])
            pending["state"] = "AUTHORIZED"
            pending["artifacts"]["assembly"] = _assembly_claim(payloads, "PENDING_PUBLICATION")
            return pending, guarded
        except (OSError, ValueError, KeyError, TypeError):
            for guard, _payload, _snapshot in reversed(guarded):
                guard.close()
    raise ValueError("ASSEMBLY_RECOVERY_REQUIRED")


def assemble_outputs(run_manifest_path: Path) -> dict:
    """Validate a complete accepted chain, then crash-safely publish final outputs."""
    _run_dir, run_path, _manifest_path = _run_paths(Path(run_manifest_path))
    entry_bytes = _read_single_link_bytes(run_path, "RUN_MANIFEST_INVALID")
    entry_run = _decode_json(entry_bytes, "RUN_MANIFEST_INVALID")
    if (
        not isinstance(entry_run, dict)
        or entry_run.get("state") != "READY_FOR_VISUAL_REVIEW"
    ):
        return _assemble_outputs(run_path)

    # READY reentry may fail even in the first semantic validation. Capture its
    # exact lifecycle before that work, and retain it through the guarded return.
    pending_run = copy.deepcopy(entry_run)
    pending_run["state"] = "AUTHORIZED"
    if isinstance(pending_run.get("artifacts"), dict):
        pending_run["artifacts"].pop("assembly", None)
    entry_snapshot = None
    entry_guard = None
    recovery_guards = []
    try:
        _path, _payload, entry_snapshot = _capture_exact_file(
            run_path, entry_bytes, "ASSEMBLY_CLAIM_INVALID"
        )
        entry_guard = _hold_exact_captured_file(
            run_path, entry_bytes, entry_snapshot, "ASSEMBLY_CLAIM_INVALID"
        )
        return _assemble_outputs(run_path, entry_bytes)
    except (OSError, ValueError) as error:
        if entry_snapshot is None:
            raise ValueError("ASSEMBLY_CLAIM_INVALID") from None
        recoverable = False
        try:
            pending_run, recovery_guards = _canonical_ready_recovery(run_path, entry_bytes)
            recoverable = True
        except (OSError, ValueError):
            # Without independently verified hashes, omit the claim. Existing
            # finals then remain non-adoptable by a subsequent assembly call.
            pass
        if entry_guard is not None:
            entry_guard.close()
            entry_guard = None
        # Bind the original snapshot across the guard-to-replacement handoff.
        # Even identical bytes on a foreign inode must never become the baseline.
        try:
            with _bind_rewrite_snapshot(run_path, entry_snapshot):
                _rewrite_existing_json(run_path, pending_run, entry_bytes)
            _verify_lifecycle_bytes(run_path, _json_bytes(pending_run))
        except (OSError, ValueError):
            raise ValueError("ASSEMBLY_CLAIM_INVALID") from None
        if not recoverable:
            raise ValueError("ASSEMBLY_RECOVERY_REQUIRED") from None
        if isinstance(error, ValueError):
            raise
        raise ValueError("ASSEMBLY_EVIDENCE_INVALID") from None
    finally:
        for guard, _payload, _snapshot in reversed(recovery_guards):
            guard.close()
        if entry_guard is not None:
            entry_guard.close()


def _assemble_outputs(run_manifest_path: Path, ready_entry_bytes: bytes | None = None) -> dict:
    (
        run_dir,
        run_path,
        run_bytes,
        run,
        manifest,
        frames,
        request_provenance,
    ) = _assembly_context(Path(run_manifest_path))
    if ready_entry_bytes is not None and run_bytes != ready_entry_bytes:
        raise ValueError("ASSEMBLY_CLAIM_INVALID")
    qa, _provenance, payloads = _assembly_payloads(
        run,
        manifest,
        frames,
        request_provenance,
    )
    expected_pending = _assembly_claim(payloads, "PENDING_PUBLICATION")
    expected_completed = _assembly_claim(payloads, "COMPLETED")
    existing_claim = run["artifacts"].get("assembly")
    claim_on_entry = existing_claim is not None
    claim_created = False
    lifecycle_guard = None
    lifecycle_snapshot = None
    guarded_evidence: list[tuple[_PathGuard, bytes, _FileSnapshot]] = []
    guarded_finals: list[tuple[_PathGuard, bytes, _FileSnapshot]] = []
    try:
        _entry_path, _entry_bytes, lifecycle_snapshot = _capture_exact_file(
            run_path,
            run_bytes,
            "ASSEMBLY_CLAIM_INVALID",
        )
        lifecycle_guard = _hold_exact_captured_file(
            run_path,
            run_bytes,
            lifecycle_snapshot,
            "ASSEMBLY_CLAIM_INVALID",
        )
        if claim_on_entry:
            if existing_claim not in (expected_pending, expected_completed):
                raise ValueError("FINAL_ARTIFACT_INVALID")
            if run["state"] == "READY_FOR_VISUAL_REVIEW":
                if existing_claim != expected_completed:
                    raise ValueError("FINAL_ARTIFACT_INVALID")
            elif existing_claim != expected_pending:
                raise ValueError("FINAL_ARTIFACT_INVALID")
        else:
            for relative in _ASSEMBLY_FINAL_PATHS:
                if os.path.lexists(run_dir / relative):
                    raise ValueError("FINAL_ARTIFACT_EXISTS")
            _verify_guarded_files(
                [(lifecycle_guard, run_bytes, lifecycle_snapshot)],
                "ASSEMBLY_CLAIM_INVALID",
            )
            lifecycle_guard.close()
            lifecycle_guard = None
            claimed_run = copy.deepcopy(run)
            claimed_run["artifacts"]["assembly"] = expected_pending
            claimed_run_bytes = _json_bytes(claimed_run)
            rewritten_snapshot = _rewrite_existing_json(
                run_path,
                claimed_run,
                run_bytes,
            )
            _claim_path, _claim_bytes, captured_claim_snapshot = _capture_exact_file(
                run_path,
                claimed_run_bytes,
                "ASSEMBLY_CLAIM_INVALID",
            )
            if not _snapshots_match(rewritten_snapshot, captured_claim_snapshot):
                raise ValueError("ASSEMBLY_CLAIM_INVALID")
            lifecycle_snapshot = rewritten_snapshot
            lifecycle_guard = _hold_exact_captured_file(
                run_path,
                claimed_run_bytes,
                lifecycle_snapshot,
                "ASSEMBLY_CLAIM_INVALID",
            )
            run = claimed_run
            run_bytes = claimed_run_bytes
            claim_created = True
            for relative in _ASSEMBLY_FINAL_PATHS:
                if os.path.lexists(run_dir / relative):
                    raise ValueError("FINAL_ARTIFACT_EXISTS")

        # Establish the evidence baseline before the authoritative semantic pass.
        captured_evidence = _capture_assembly_evidence(run_dir, run, manifest)
        _verify_captured_files(captured_evidence, "ASSEMBLY_EVIDENCE_INVALID")
        _verify_guarded_files(
            [(lifecycle_guard, run_bytes, lifecycle_snapshot)],
            "ASSEMBLY_CLAIM_INVALID",
        )
        (
            verified_dir,
            verified_path,
            verified_run_bytes,
            verified_run,
            verified_manifest,
            verified_frames,
            verified_requests,
        ) = _assembly_context(run_path)
        verified_qa, _verified_provenance, verified_payloads = _assembly_payloads(
            verified_run,
            verified_manifest,
            verified_frames,
            verified_requests,
        )
        _verify_captured_files(captured_evidence, "ASSEMBLY_EVIDENCE_INVALID")
        _verify_guarded_files(
            [(lifecycle_guard, run_bytes, lifecycle_snapshot)],
            "ASSEMBLY_CLAIM_INVALID",
        )
        expected_verified_claim = (
            expected_completed
            if verified_run["state"] == "READY_FOR_VISUAL_REVIEW"
            else expected_pending
        )
        if (
            verified_dir != run_dir
            or verified_path != run_path
            or verified_run_bytes != run_bytes
            or verified_run["artifacts"].get("assembly") != expected_verified_claim
            or verified_qa != qa
            or verified_payloads != payloads
        ):
            raise ValueError("FINAL_ARTIFACT_INVALID")
        run = verified_run
        manifest = verified_manifest

        for path, evidence_bytes, evidence_snapshot in captured_evidence:
            evidence_guard = _hold_exact_captured_file(
                path,
                evidence_bytes,
                evidence_snapshot,
                "ASSEMBLY_EVIDENCE_INVALID",
            )
            guarded_evidence.append(
                (evidence_guard, evidence_bytes, evidence_snapshot)
            )
        _verify_guarded_files(guarded_evidence, "ASSEMBLY_EVIDENCE_INVALID")

        if run["state"] == "READY_FOR_VISUAL_REVIEW":
            for relative in _ASSEMBLY_FINAL_PATHS:
                path, payload, snapshot = _capture_exact_file(
                    run_dir / relative,
                    payloads[relative],
                    "FINAL_ARTIFACT_INVALID",
                )
                final_guard = _hold_exact_assembly_final(path, payload, snapshot)
                guarded_finals.append((final_guard, payload, snapshot))
            _verify_lifecycle_bytes(run_path, run_bytes)
            return _verified_ready_assembly_result(
                run_path,
                run_bytes,
                run,
                lifecycle_guard,
                guarded_evidence,
                guarded_finals,
                qa,
            )

        pending_run = copy.deepcopy(run)
        for relative in _ASSEMBLY_FINAL_PATHS:
            _verify_captured_files(captured_evidence, "ASSEMBLY_EVIDENCE_INVALID")
            _verify_guarded_files(guarded_evidence, "ASSEMBLY_EVIDENCE_INVALID")
            _verify_guarded_assembly_finals(guarded_finals)
            _verify_guarded_files(
                [(lifecycle_guard, run_bytes, lifecycle_snapshot)],
                "ASSEMBLY_CLAIM_INVALID",
            )
            published_snapshot = _publish_or_recover_assembly(
                run_dir / relative,
                payloads[relative],
                claim_on_entry and not claim_created,
            )
            final_guard = _hold_exact_assembly_final(
                run_dir / relative,
                payloads[relative],
                published_snapshot,
            )
            guarded_finals.append(
                (final_guard, payloads[relative], published_snapshot)
            )
            _verify_guarded_files(guarded_evidence, "ASSEMBLY_EVIDENCE_INVALID")
            _verify_guarded_assembly_finals(guarded_finals)

        final_run = copy.deepcopy(run)
        final_run["artifacts"]["assembly"] = expected_completed
        final_run["state"] = "READY_FOR_VISUAL_REVIEW"
        final_run["terminal_reason"] = None
        final_run_bytes = _json_bytes(final_run)
        _verify_guarded_files(guarded_evidence, "ASSEMBLY_EVIDENCE_INVALID")
        _verify_guarded_assembly_finals(guarded_finals)
        _verify_guarded_files(
            [(lifecycle_guard, run_bytes, lifecycle_snapshot)],
            "ASSEMBLY_CLAIM_INVALID",
        )
        lifecycle_guard.close()
        lifecycle_guard = None
        try:
            ready_snapshot = _rewrite_existing_json(run_path, final_run, run_bytes)
            lifecycle_snapshot = ready_snapshot
            lifecycle_guard = _hold_exact_captured_file(
                run_path,
                final_run_bytes,
                lifecycle_snapshot,
                "ASSEMBLY_CLAIM_INVALID",
            )
            _verify_lifecycle_bytes(run_path, final_run_bytes)
            _verify_captured_files(captured_evidence, "ASSEMBLY_EVIDENCE_INVALID")
            return _verified_ready_assembly_result(
                run_path,
                final_run_bytes,
                final_run,
                lifecycle_guard,
                guarded_evidence,
                guarded_finals,
                qa,
            )
        except (OSError, ValueError) as error:
            if lifecycle_guard is not None:
                lifecycle_guard.close()
                lifecycle_guard = None
            _restore_pending_assembly(run_path, pending_run, final_run_bytes)
            if isinstance(error, ValueError):
                raise
            raise ValueError("FINAL_ARTIFACT_INVALID") from None
    except OSError:
        raise ValueError("ASSEMBLY_EVIDENCE_INVALID") from None
    finally:
        for guard, _payload, _snapshot in reversed(guarded_finals):
            guard.close()
        for guard, _payload, _snapshot in reversed(guarded_evidence):
            guard.close()
        if lifecycle_guard is not None:
            lifecycle_guard.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    for name in (
        "source",
        "movement-plan",
        "pricing",
        "animation",
        "direction",
        "max-budget-usd",
        "workspace-root",
        "run-id",
    ):
        prepare.add_argument(f"--{name}", required=True)
    prepare.add_argument("--frame-count", type=int, required=True)
    prepare.add_argument("--frame-width", type=int)
    prepare.add_argument("--frame-height", type=int)
    summary = commands.add_parser("summary")
    summary.add_argument("--run-manifest", required=True)
    authorize = commands.add_parser("authorize")
    authorize.add_argument("--run-manifest", required=True)
    authorize.add_argument("--authorized-budget-usd", required=True)
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--run-manifest", required=True)
    evaluate.add_argument("--frame-index", type=int, required=True)
    evaluate.add_argument("--raw", required=True)
    assemble = commands.add_parser("assemble")
    assemble.add_argument("--run-manifest", required=True)
    next_request = commands.add_parser("next-request")
    next_request.add_argument("--run-manifest", required=True)
    next_request.add_argument("--output", required=True)
    return parser


def main() -> None:
    parser = _build_parser()
    arguments = parser.parse_args()
    if arguments.command == "summary":
        print(
            json.dumps(
                run_authorization_summary(Path(arguments.run_manifest)),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
        return
    if arguments.command == "authorize":
        try:
            authorized_budget_usd = Decimal(arguments.authorized_budget_usd)
        except (InvalidOperation, ValueError):
            raise ValueError("AUTHORIZED_BUDGET_MISMATCH") from None
        print(
            json.dumps(
                authorize_run(
                    Path(arguments.run_manifest),
                    authorized_budget_usd,
                ),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
        return
    if arguments.command == "evaluate":
        print(
            json.dumps(
                record_evaluation(
                    Path(arguments.run_manifest),
                    arguments.frame_index,
                    Path(arguments.raw),
                ),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
        return
    if arguments.command == "assemble":
        print(
            json.dumps(
                assemble_outputs(Path(arguments.run_manifest)),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
        return
    if arguments.command == "next-request":
        print(
            json.dumps(
                next_request_plan(
                    Path(arguments.run_manifest),
                    Path(arguments.output),
                ),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
        return
    if (arguments.frame_width is None) != (arguments.frame_height is None):
        parser.error("frame width and height must appear together")
    frame_size = None
    if arguments.frame_width is not None:
        frame_size = (arguments.frame_width, arguments.frame_height)
    try:
        max_budget_usd = Decimal(arguments.max_budget_usd)
    except (InvalidOperation, ValueError):
        raise ValueError("MANIFEST_INVALID") from None
    run_dir = prepare_run(
        source=Path(arguments.source),
        movement_plan_path=Path(arguments.movement_plan),
        pricing_path=Path(arguments.pricing),
        animation=arguments.animation,
        direction=arguments.direction,
        frame_count=arguments.frame_count,
        frame_size=frame_size,
        max_budget_usd=max_budget_usd,
        workspace_root=Path(arguments.workspace_root),
        run_id=arguments.run_id,
    )
    print(run_dir)


if __name__ == "__main__":
    try:
        main()
    except (GenericImageBlocked, ValueError, OSError) as error:
        reason = str(error)
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", reason) is None:
            reason = "RUN_MANIFEST_INVALID"
        print(
            json.dumps(
                {"state": "BLOCKED", "reason": reason},
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
        sys.exit(2)
