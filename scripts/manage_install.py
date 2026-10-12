#!/usr/bin/env python3
"""Install Markdown Reader for one user or an isolated prefix."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

APP_ID = "local.markdownreader.Reader"
DESKTOP_ID = APP_ID + ".desktop"
MIME_TYPES = ("text/markdown", "text/x-markdown")
OWNER = "markdown-reader-user-install-v1"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def desktop_quote(value: str) -> str:
    # Desktop Exec quoting is not shell quoting. Percent signs are field codes.
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("`", "\\`").replace("$", "\\$").replace("%", "%%")
    return '"' + escaped + '"'


class Installation:
    def __init__(self, prefix: Path | None, source: Path | None = None):
        self.staging = prefix is not None
        self.prefix = (prefix or Path.home() / ".local").absolute()
        self.data = self.prefix / "share" if self.staging else Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
        self.config = self.prefix / "config" if self.staging else Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        self.root = self.data / "markdown-reader"
        self.app = self.root / "app"
        self.state_path = self.root / "install-state.json"
        self.source = (source or Path(__file__).resolve().parents[1]).resolve()

    def state(self) -> dict:
        if not self.state_path.exists():
            return {"owner": OWNER, "files": {}}
        result = json.loads(self.state_path.read_text())
        if result.get("owner") != OWNER:
            raise RuntimeError("Installation state belongs to another application.")
        return result

    def write_state(self, state: dict) -> None:
        atomic_write(self.state_path, (json.dumps(state, indent=2) + "\n").encode())

    def allowed(self, path: Path) -> bool:
        # Each owned output is below one of these exact app-specific locations.
        return path == self.root / "manage_install.py" or path.is_relative_to(self.app) or path in {
            self.prefix / "bin/markdown-reader", self.prefix / "bin/markdown-reader-uninstall",
            self.data / "applications" / DESKTOP_ID,
            self.data / "icons/hicolor/scalable/apps" / (APP_ID + ".svg"),
            self.data / "mime/packages" / (APP_ID + ".xml"),
        }

    def check_path(self, path: Path) -> None:
        if path != Path(os.path.normpath(path)) or not self.allowed(path):
            raise RuntimeError(f"Refusing an unowned installation path: {path}")
        # Never follow a directory symlink into unrelated data while replacing files.
        for parent in (path, *path.parents):
            if parent.is_symlink():
                raise RuntimeError(f"Refusing a symlink in installation path: {parent}")
            if parent == self.prefix or parent == self.data:
                break

    def refresh_databases(self) -> None:
        # These tools only update the supplied user-owned directories.
        for executable, directory in (("update-desktop-database", self.data / "applications"),
                                      ("update-mime-database", self.data / "mime")):
            if shutil.which(executable) and directory.exists():
                environment = dict(os.environ, XDG_DATA_HOME=str(self.data))
                subprocess.run([executable, str(directory)], check=True, stdout=subprocess.DEVNULL, env=environment)

    def install(self) -> None:
        if not (self.source / "markdown_reader/__main__.py").is_file():
            raise RuntimeError("Native application is missing; install from the completed source project.")
        if not (self.source / "frontend/dist/index.html").is_file():
            raise RuntimeError("Bundled frontend is missing. Obtain a complete application checkout.")
        if not self.staging:
            runtime = subprocess.run(['/usr/bin/python3', '-m', 'markdown_reader', '--check-runtime'],
                                     cwd=self.source, capture_output=True, text=True)
            if runtime.returncode:
                raise RuntimeError(runtime.stdout.strip() or runtime.stderr.strip() or 'Native runtime is unavailable.')
        if self.root.exists() and not self.state_path.exists() and any(self.root.iterdir()):
            raise RuntimeError(f"Refusing an existing unowned directory: {self.root}")
        state = self.state()
        if "registration_defaults" not in state:
            state["registration_defaults"] = {mime: self.query_default(mime) for mime in MIME_TYPES}
        payload: dict[Path, tuple[bytes, int]] = {}
        for folder in ("markdown_reader", "frontend/dist"):
            for path in sorted((self.source / folder).rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                    if path.is_symlink():
                        raise RuntimeError(f"Source payload contains a symlink: {path}")
                    payload[self.app / path.relative_to(self.source)] = (path.read_bytes(), 0o644)
        for name in ("LICENSE", "README.md"):
            payload[self.app / name] = ((self.source / name).read_bytes(), 0o644)
        payload[self.root / "manage_install.py"] = (Path(__file__).read_bytes(), 0o644)
        # cd is safe for the runtime, but resolve caller-relative arguments first.
        launcher = "#!/usr/bin/python3\nimport os, sys\nargs = []\nfiles_only = False\nfor p in sys.argv[1:]:\n    if p == '--':\n        files_only = True\n    args.append(os.path.abspath(p) if p != '--' and p and (files_only or not p.startswith('-')) and not p.startswith('file:') else p)\nos.chdir(" + repr(str(self.app)) + ")\nos.execv('/usr/bin/python3', ['/usr/bin/python3', '-m', 'markdown_reader', *args])\n"
        prefix_args = " --prefix " + shlex.quote(str(self.prefix)) if self.staging else ""
        uninstall = "#!/bin/sh\nexec /usr/bin/python3 " + shlex.quote(str(self.root / "manage_install.py")) + prefix_args + " uninstall \"$@\"\n"
        payload[self.prefix / "bin/markdown-reader"] = (launcher.encode(), 0o755)
        payload[self.prefix / "bin/markdown-reader-uninstall"] = (uninstall.encode(), 0o755)
        desktop = ("[Desktop Entry]\nType=Application\nVersion=1.0\nName=Markdown Reader\n"
                   "Comment=Read and edit beautifully rendered Markdown documents\n"
                   f"Exec={desktop_quote(str(self.prefix / 'bin/markdown-reader'))} %F\n"
                   f"Icon={APP_ID}\nTerminal=false\nStartupNotify=true\n"
                   "Categories=Office;Viewer;\nMimeType=text/markdown;text/x-markdown;\n"
                   "Keywords=Markdown;Reader;Preview;\n")
        payload[self.data / "applications" / DESKTOP_ID] = (desktop.encode(), 0o644)
        payload[self.data / "icons/hicolor/scalable/apps" / (APP_ID + ".svg")] = ((self.source / "assets/icon.svg").read_bytes(), 0o644)
        mime = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">\n'
                '  <mime-type type="text/markdown"><comment>Markdown document</comment>'
                '<sub-class-of type="text/plain"/><glob pattern="*.md"/>'
                '<glob pattern="*.markdown"/></mime-type>\n</mime-info>\n')
        payload[self.data / "mime/packages" / (APP_ID + ".xml")] = (mime.encode(), 0o644)
        # Validate the entire replacement before writing any application files.
        for path, (data, _mode) in payload.items():
            self.check_path(path)
            if path.exists():
                current = digest(path.read_bytes())
                if current != state["files"].get(str(path)) and current != digest(data):
                    raise RuntimeError(f"Refusing to overwrite an unowned or modified file: {path}")
        self.write_state(state)
        for path, (data, mode) in payload.items():
            atomic_write(path, data, mode)
            state["files"][str(path)] = digest(data)
            self.write_state(state)
        state["app_id"] = APP_ID
        self.write_state(state)
        # Retire prior builds only after the replacement is complete. Ownership
        # and hashes protect user modifications and unrelated installation data.
        for filename, expected_digest in list(state["files"].items()):
            path = Path(filename)
            if path in payload:
                continue
            try:
                self.check_path(path)
                if path.exists():
                    if not path.is_file() or digest(path.read_bytes()) != expected_digest:
                        print(f"Retained modified obsolete file: {path}")
                        continue
                    path.unlink()
                del state["files"][filename]
                self.write_state(state)
            except (OSError, RuntimeError) as error:
                print(f"Retained obsolete file: {path}: {error}")
        self.refresh_databases()
        print(f"Installed Markdown Reader: {self.prefix / 'bin/markdown-reader'}")
        print("Markdown defaults were not changed. Run the set-default command after verification.")

    def query_default(self, mime: str) -> str:
        if self.staging:
            path = self.config / "mimeapps.list"
            section = ""
            for line in path.read_text().splitlines() if path.exists() else []:
                if line.startswith("["):
                    section = line.strip()
                elif section == "[Default Applications]" and line.startswith(mime + "="):
                    return line.partition("=")[2].split(";")[0].strip()
            return ""
        return subprocess.check_output(["xdg-mime", "query", "default", mime], text=True).strip()

    def edit_association(self, path: Path, mime: str, value: str | None) -> None:
        """Change one default line while retaining unrelated configuration and comments."""
        lines = path.read_text().splitlines(keepends=True) if path.exists() else []
        output: list[str] = []
        in_defaults = False
        found_section = False
        handled = False
        for line in lines:
            if line.lstrip().startswith("["):
                if in_defaults and not handled and value:
                    output.append(f"{mime}={value};\n")
                    handled = True
                in_defaults = line.strip() == "[Default Applications]"
                found_section |= in_defaults
            if in_defaults and line.startswith(mime + "="):
                if not handled and value:
                    output.append(f"{mime}={value};\n")
                handled = True
            else:
                output.append(line if line.endswith("\n") else line + "\n")
        if not handled and value:
            if not found_section:
                output.append("\n[Default Applications]\n")
            output.append(f"{mime}={value};\n")
        atomic_write(path, "".join(output).encode())

    def assign_default(self, mime: str, value: str) -> None:
        if self.staging:
            self.edit_association(self.config / "mimeapps.list", mime, value)
        else:
            self.config.mkdir(parents=True, exist_ok=True)
            subprocess.run(["xdg-mime", "default", value, mime], check=True)

    def set_default(self) -> None:
        state = self.state()
        desktop_path = self.data / "applications" / DESKTOP_ID
        if str(desktop_path) not in state["files"] or not desktop_path.exists():
            raise RuntimeError("Install the application before assigning Markdown defaults.")
        if "previous_defaults" not in state:
            previous = {mime: self.query_default(mime) for mime in MIME_TYPES}
            # A desktop may choose a newly registered app as an implicit fallback.
            state["previous_defaults"] = {
                mime: state.get("registration_defaults", {}).get(mime, "") if value == DESKTOP_ID else value
                for mime, value in previous.items()
            }
            self.write_state(state)  # Recovery record is durable before changing either handler.
        for mime in MIME_TYPES:
            self.assign_default(mime, DESKTOP_ID)
            if self.query_default(mime) != DESKTOP_ID:
                raise RuntimeError(f"Desktop did not accept the new handler for {mime}.")
        print("Markdown Reader is the default for .md and .markdown documents.")

    def uninstall(self) -> None:
        if not self.state_path.exists():
            print("No owned installation was found; nothing was removed.")
            return
        state = self.state()
        for mime, previous in state.get("previous_defaults", {}).items():
            if mime not in MIME_TYPES or self.query_default(mime) != DESKTOP_ID:
                continue
            if previous and previous != DESKTOP_ID:
                self.assign_default(mime, previous)
            else:
                for path in (self.config / "mimeapps.list", self.data / "applications/mimeapps.list"):
                    if path.exists():
                        self.edit_association(path, mime, None)
        preserved: dict[str, str] = {}
        parents: set[Path] = set()
        for name, expected in state["files"].items():
            path = Path(name)
            self.check_path(path)
            if not path.exists():
                continue
            if digest(path.read_bytes()) != expected:
                print(f"Preserved modified file: {path}")
                preserved[name] = expected
                continue
            path.unlink()
            parents.update(p for p in path.parents if p == self.root or p.is_relative_to(self.root))
        # rmdir only succeeds for empty directories; unknown files can never be removed.
        for parent in sorted(parents, key=lambda p: len(p.parts), reverse=True):
            try:
                parent.rmdir()
            except OSError:
                pass
        if preserved:
            state["files"] = preserved
            self.write_state(state)
        else:
            self.state_path.unlink(missing_ok=True)
            try:
                self.root.rmdir()
            except OSError:
                pass
        self.refresh_databases()
        print("Uninstalled owned application files. Documents, settings, and recovery drafts were preserved.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, help="Isolated installation prefix; MIME state stays below PREFIX/config")
    parser.add_argument("--source", type=Path, help="Source project (defaults to this script's parent project)")
    parser.add_argument("command", choices=("install", "set-default", "uninstall"))
    args = parser.parse_args()
    try:
        installation = Installation(args.prefix, args.source)
        getattr(installation, args.command.replace("-", "_"))()
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Markdown Reader: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
