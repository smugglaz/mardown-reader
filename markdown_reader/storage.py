"""Local Markdown files and durable per-user application state.

The editor receives LF-normalized text. Disk snapshots retain enough information
to preserve the source encoding/newlines and reject stale writes. Nothing in this
module changes a file merely by reading it.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any
from urllib.parse import unquote, urlsplit

from gi.repository import Gio, GLib


APP_ID = "local.markdownreader.Reader"


class ConflictError(RuntimeError):
    """The destination no longer has the version the user opened."""


class ReadOnlyError(RuntimeError):
    """Writing would damage unsupported text or violate file permissions."""


@dataclass(frozen=True)
class DiskSnapshot:
    path: Path
    canonical_path: Path
    text: str
    etag: str | None
    digest: str
    encoding: str = "utf-8"
    newline: str = "\n"
    bom: bool = False
    editable: bool = True
    warning: str | None = None

    @property
    def identity(self) -> str:
        return str(self.canonical_path)

    @property
    def base_dir(self) -> Path:
        return self.path.parent


def local_path(path_or_uri: str | os.PathLike[str]) -> Path:
    """Return an absolute local path without resolving symlinks.

    Percent escapes are decoded only for file URIs. A literal path containing
    '%' or '#' must keep its exact spelling.
    """
    value = os.fspath(path_or_uri)
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("A nonempty local filename is required.")
    if value.lower().startswith("file:"):
        parsed = urlsplit(value)
        if parsed.netloc not in ("", "localhost") or parsed.query or parsed.fragment:
            raise ValueError("Only local file URIs without query or fragment are supported.")
        file = Gio.File.new_for_uri(value)
        filename = file.get_path()
        if filename is None:
            raise ValueError("The URI does not identify a local file.")
        value = filename
    elif "://" in value:
        raise ValueError("Only local files can be opened as documents.")
    # abspath preserves symbolic links but makes later relative-path use stable.
    return Path(os.path.abspath(os.path.expanduser(value)))


def _digest(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def _decode(contents: bytes) -> tuple[str, str, str, bool, bool, str | None]:
    bom = contents.startswith(b"\xef\xbb\xbf")
    payload = contents[3:] if bom else contents
    encoding, editable, warning = "utf-8", True, None
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        text = payload.decode("utf-8", errors="replace")
        encoding, editable = "unknown", False
        warning = "This file is not valid UTF-8. It is displayed with replacement characters and cannot be edited."
    if "\0" in text:
        editable = False
        warning = "This file contains binary data or an unsupported encoding and cannot be edited."
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    styles = sum(count > 0 for count in (crlf, lf, cr))
    newline = "\r\n" if crlf else "\r" if cr and not lf else "\n"
    if styles > 1:
        editable = False
        warning = "This file uses mixed line endings. Reading is available, but editing is disabled to preserve the original bytes."
    return text.replace("\r\n", "\n").replace("\r", "\n"), encoding, newline, bom, editable, warning


def read_document(path_or_uri: str | os.PathLike[str]) -> DiskSnapshot:
    path = local_path(path_or_uri)
    canonical = path.resolve(strict=True)
    if not canonical.is_file():
        raise ValueError(f"Not a regular file: {path}")
    ok, contents, etag = Gio.File.new_for_path(str(canonical)).load_contents(None)
    if not ok:
        raise OSError(f"Could not read {path}")
    # Reading through the old resolved target must not mask a retargeted link.
    if path.resolve(strict=True) != canonical:
        raise ConflictError("The symbolic link changed while this document was being read. Please open it again.")
    data = bytes(contents)
    text, encoding, newline, bom, editable, warning = _decode(data)
    return DiskSnapshot(path, canonical, text, etag, _digest(data), encoding, newline, bom, editable, warning)


def _assert_current(snapshot: DiskSnapshot) -> DiskSnapshot:
    try:
        current = read_document(snapshot.path)
    except (FileNotFoundError, OSError, GLib.Error, ValueError) as exc:
        raise ConflictError("The original file is missing or cannot be read. Reload it or save a copy.") from exc
    if current.canonical_path != snapshot.canonical_path:
        raise ConflictError("The symbolic link now points to another file. Reopen it or save a copy.")
    if current.digest != snapshot.digest or current.etag != snapshot.etag:
        raise ConflictError("This document changed on disk. Compare the versions, reload, or save a copy.")
    return current


def _assert_writable(snapshot: DiskSnapshot) -> None:
    if not snapshot.editable:
        raise ReadOnlyError(snapshot.warning or "This file cannot be edited safely.")
    mode = snapshot.canonical_path.stat().st_mode
    if not (mode & 0o222) or not os.access(snapshot.canonical_path, os.W_OK):
        raise ReadOnlyError("The document is read-only. Choose Save a Copy to use another location.")


def _encode(snapshot: DiskSnapshot, text: str) -> bytes:
    if not snapshot.editable:
        raise ReadOnlyError(snapshot.warning or "This file cannot be edited safely.")
    if not isinstance(text, str) or "\0" in text:
        raise ValueError("Document contents must be text without NUL characters.")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    payload = normalized.replace("\n", snapshot.newline).encode("utf-8")
    return (b"\xef\xbb\xbf" if snapshot.bom else b"") + payload


def _replace_existing(snapshot: DiskSnapshot, payload: bytes) -> DiskSnapshot:
    _assert_writable(snapshot)
    _assert_current(snapshot)
    # Gio validates the etag again during its atomic replacement. The preceding
    # SHA-256 check also catches writes which preserve the timestamp/size.
    try:
        ok, _etag = Gio.File.new_for_path(str(snapshot.canonical_path)).replace_contents(
            payload, snapshot.etag, False, Gio.FileCreateFlags.NONE, None
        )
    except GLib.Error as exc:
        if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.WRONG_ETAG):
            raise ConflictError("This document changed while saving. Your edits are still available; save a copy.") from exc
        if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.PERMISSION_DENIED):
            raise ReadOnlyError("The document cannot be written. Choose Save a Copy.") from exc
        raise
    if not ok:
        raise OSError(f"Could not save {snapshot.path}")
    result = read_document(snapshot.path)
    if result.canonical_path != snapshot.canonical_path or result.digest != _digest(payload):
        raise ConflictError("The document changed again immediately after saving. Reload it to inspect the current disk version.")
    return result


def save_document(snapshot: DiskSnapshot, text: str) -> DiskSnapshot:
    """Save only if the exact opened version is still on disk.

    No-op saves do not replace the file, normalize bytes, or change its mtime.
    Callers must retain the returned snapshot after every successful save.
    """
    current = _assert_current(snapshot)
    if text == snapshot.text:
        return current
    return _replace_existing(snapshot, _encode(snapshot, text))


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_atomic(path: Path, contents: bytes, *, exclusive: bool = False, mode: int = 0o600) -> None:
    """Write beside the target, then publish atomically; clean up on failure."""
    fd, filename = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            # link() publishes a completely written file and fails atomically if
            # any destination (including a broken symlink) already exists.
            os.link(temporary, path)
            temporary.unlink()
        else:
            os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def save_copy(
    snapshot: DiskSnapshot | None,
    text: str,
    destination: str | os.PathLike[str],
    expected_snapshot: DiskSnapshot | None = None,
) -> DiskSnapshot:
    """Save a copy, refusing to overwrite an unconfirmed or changed target.

    A None snapshot denotes an untitled UTF-8/LF document without a BOM.
    The caller rebases relative references before passing text. The source may
    have changed on disk: saving the user's retained buffer as a copy is safe.
    An existing destination needs a freshly read, explicitly confirmed snapshot.
    """
    path = local_path(destination)
    source = snapshot or DiskSnapshot(path, path.resolve(strict=False), "", None, "")
    payload = _encode(source, text)
    if expected_snapshot is not None:
        if path.resolve(strict=False) != expected_snapshot.canonical_path:
            raise ConflictError("The selected destination does not match the confirmed file.")
        _assert_current(expected_snapshot)
        _replace_existing(expected_snapshot, payload)
        return read_document(path)
    try:
        _write_atomic(path, payload, exclusive=True)
    except FileExistsError as exc:
        raise ConflictError("The destination already exists. Confirm that version before replacing it, or choose a new filename.") from exc
    return read_document(path)


def resolve_local_reference(base_path: str | os.PathLike[str], href: str) -> tuple[Path, str] | None:
    """Resolve a document reference without allowing remote file authorities.

    The result retains symlink spelling so relative assets follow the location
    through which the document was opened. Web/mail/data links return None.
    Fragments are URL-decoded separately from the filename.
    """
    if not isinstance(href, str) or "\0" in href:
        return None
    try:
        parsed = urlsplit(href)
    except ValueError:
        return None
    if parsed.scheme and parsed.scheme.lower() != "file":
        return None
    if parsed.netloc and not (parsed.scheme.lower() == "file" and parsed.netloc == "localhost"):
        return None
    base = local_path(base_path)
    if parsed.scheme.lower() == "file":
        target = Path(unquote(parsed.path))
        if not target.is_absolute():
            return None
    elif not parsed.path:
        target = base
    else:
        target = Path(unquote(parsed.path))
        if not target.is_absolute():
            target = base.parent / target
    if "\0" in str(target):
        return None
    return Path(os.path.abspath(target)), unquote(parsed.fragment)


class StateStore:
    """Atomic JSON session data and crash-recovery drafts, private to the user."""

    def __init__(self, base_dir: str | os.PathLike[str] | None = None):
        state_home = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        self.base_dir = Path(base_dir) if base_dir is not None else state_home / APP_ID
        self.drafts_dir = self.base_dir / "drafts"

    def _ensure_directory(self, path: Path) -> None:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _save_json(self, path: Path, value: Any) -> None:
        contents = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        self._ensure_directory(path.parent)
        _write_atomic(path, contents)

    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None

    def load_session(self) -> dict[str, Any]:
        value = self._read_json(self.base_dir / "session.json")
        return value if isinstance(value, dict) else {}

    def save_session(self, session: dict[str, Any]) -> None:
        if not isinstance(session, dict):
            raise TypeError("Session data must be an object.")
        self._save_json(self.base_dir / "session.json", session)

    def _draft_path(self, docid: str) -> Path:
        if not isinstance(docid, str) or not docid:
            raise ValueError("A nonempty document ID is required.")
        name = hashlib.sha256(docid.encode("utf-8")).hexdigest()
        return self.drafts_dir / f"{name}.json"

    def write_draft(self, docid: str, metadata: dict[str, Any]) -> None:
        if not isinstance(metadata, dict):
            raise TypeError("Draft metadata must be an object.")
        self._save_json(self._draft_path(docid), {"docid": docid, "metadata": metadata})

    def remove_draft(self, docid: str) -> None:
        path = self._draft_path(docid)
        if path.exists():
            path.unlink()
            _fsync_directory(path.parent)

    def load_drafts(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for path in sorted(self.drafts_dir.glob("*.json")):
            value = self._read_json(path)
            if not isinstance(value, dict):
                continue
            docid, metadata = value.get("docid"), value.get("metadata")
            if isinstance(docid, str) and docid and isinstance(metadata, dict) and path == self._draft_path(docid):
                result[docid] = metadata
        return result
