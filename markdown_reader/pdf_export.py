"""Print the already-rendered document with an isolated local browser process.

WebKitGTK currently omits PDF link annotations (WebKit bug 302265). Chromium's
print engine retains them. This module loads no document scripts or remote page;
it embeds the app's styles, fonts and referenced images in a private static file.
"""
from __future__ import annotations

import base64
import html
import hashlib
from html.parser import HTMLParser
import mimetypes
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .storage import Gio, resolve_local_reference

MAX_IMAGE = 40 * 1024 * 1024


def browser_path():
    for name in ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome"):
        if executable := shutil.which(name):
            return executable
    raise RuntimeError("PDF export needs Chromium or Google Chrome. Reading and editing remain available.")


def data_url(data: bytes, mime: str) -> str:
    return "data:" + mime + ";base64," + base64.b64encode(data).decode("ascii")


def bundled_styles(frontend: Path) -> str:
    class Stylesheets(HTMLParser):
        def __init__(self):
            super().__init__()
            self.paths = []
        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            if tag == "link" and "stylesheet" in values.get("rel", "").split():
                source = urlsplit(values.get("href", ""))
                target = (frontend / source.path).resolve()
                if source.scheme or source.netloc or not target.is_relative_to(frontend.resolve()) or not target.is_file():
                    raise ValueError("A bundled print stylesheet is unavailable")
                if target not in self.paths:
                    self.paths.append(target)
    manifest = Stylesheets()
    manifest.feed((frontend / "index.html").read_text())
    sheets = []
    for path in manifest.paths:
        def asset(match):
            source = match[1].strip(' "\'')
            if source.startswith("data:"):
                return match[0]
            target = (path.parent / source).resolve()
            if not target.is_relative_to(frontend.resolve()) or not target.is_file():
                raise ValueError("A bundled print resource is unavailable")
            mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            return 'url("' + data_url(target.read_bytes(), mime) + '")'
        sheets.append(re.sub(r"url\(([^)]+)\)", asset, path.read_text()))
    if not sheets:
        raise ValueError("The print stylesheet is missing")
    return "\n".join(sheets)


class StaticDocument(HTMLParser):
    def __init__(self, base_path: Path, resources: dict, allow_missing: bool):
        super().__init__(convert_charrefs=False)
        self.base_path, self.resources, self.allow_missing = base_path, resources, allow_missing
        self.parts = []
        self.images = {}

    def image(self, source):
        if not source or source.startswith("data:image/"):
            return source
        if source in self.images:
            return self.images[source]
        parsed = urlsplit(source)
        if parsed.scheme == "reader" and parsed.netloc == "asset":
            path = self.resources.get(parsed.path.lstrip("/"))
            if path is None:
                raise ValueError("An image resource is no longer available")
            if path.stat().st_size > MAX_IMAGE:
                raise ValueError("An image exceeds 40 MB")
            raw = path.read_bytes()
            mime = mimetypes.guess_type(path.name)[0] or ""
        elif parsed.scheme in ("http", "https"):
            request = Request(source, headers={"User-Agent": "MarkdownReader/0.1", "Accept": "image/*"})
            with urlopen(request, timeout=12) as response:
                if urlsplit(response.url).scheme not in ("http", "https"):
                    raise ValueError("Unsupported image redirect")
                mime = response.headers.get_content_type()
                raw = response.read(MAX_IMAGE + 1)
        else:
            resolved = resolve_local_reference(self.base_path, source)
            if not resolved:
                raise ValueError("Unsupported image address")
            path = resolved[0]
            if path.stat().st_size > MAX_IMAGE:
                raise ValueError("An image exceeds 40 MB")
            raw = path.read_bytes()
            mime = mimetypes.guess_type(path.name)[0] or ""
        if len(raw) > MAX_IMAGE or not mime.startswith("image/"):
            raise ValueError("An image cannot be embedded safely")
        result = data_url(raw, mime)
        self.images[source] = result
        return result

    def handle_starttag(self, tag, attributes):
        text = self.get_starttag_text()
        values = dict(attributes)
        replacements = {}
        if tag in ("img", "image"):
            for key in ("src", "href", "xlink:href"):
                if values.get(key):
                    try:
                        replacements[key] = self.image(values[key])
                    except Exception as exc:
                        if not self.allow_missing:
                            raise ValueError(f"An image could not be prepared for PDF: {exc}") from exc
                        replacements[key] = ""
        if tag == "a" and values.get("href"):
            href = values["href"]
            if not href.startswith("#") and urlsplit(href).scheme in ("", "file"):
                resolved = resolve_local_reference(self.base_path, href)
                if resolved:
                    from urllib.parse import quote
                    replacements["href"] = resolved[0].as_uri() + ("#" + quote(resolved[1]) if resolved[1] else "")
        # Browser-serialized DOM uses quoted attributes and escapes embedded quotes.
        def replace(match):
            name = match[1].lower()
            return (match[1] + '="' + html.escape(replacements[name], quote=True) + '"') if name in replacements else match[0]
        text = re.sub(r'''\b([\w:-]+)=("[^"]*"|'[^']*')''', replace, text)
        self.parts.append(text)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        self.parts.append("</" + tag + ">")

    def handle_data(self, data):
        self.parts.append(data)

    def handle_entityref(self, name):
        self.parts.append("&" + name + ";")

    def handle_charref(self, name):
        self.parts.append("&#" + name + ";")


def export_pdf(payload: dict, output: Path, paper: str, frontend: Path,
               base_path: Path, resources: dict, allow_missing=False):
    executable = browser_path()
    output = output.absolute()
    if output.suffix.lower() != ".pdf":
        raise ValueError("Choose a filename ending in .pdf")
    if output.is_symlink() or output.resolve() == base_path.resolve():
        raise ValueError("Choose a separate PDF destination; the source document must stay intact")
    resolved_output = output.resolve()
    file = Gio.File.new_for_path(str(output))
    expected = file.query_info("etag::value", Gio.FileQueryInfoFlags.NONE, None).get_etag() if output.exists() else None
    original_digest = hashlib.sha256(output.read_bytes()).digest() if output.exists() else None
    snapshot = StaticDocument(base_path, resources, allow_missing)
    snapshot.feed(payload.get("html", ""))
    if not snapshot.parts:
        raise ValueError("The document did not supply printable content")
    styles = bundled_styles(frontend)
    size = "Letter" if paper == "na_letter" else "A4"
    styles += f"\n@page{{size:{size};margin:16mm}} @media print{{*{{-webkit-print-color-adjust:exact;print-color-adjust:exact}}}}"
    # CSP disables all script, network, objects and frames in the print process.
    csp = "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; object-src 'none'; frame-src 'none'; base-uri 'none'"
    document = ("<!doctype html><html data-theme=light><head><meta charset=utf-8>"
                '<meta http-equiv="Content-Security-Policy" content="' + csp + '">'
                "<title>" + html.escape(payload.get("title", "Markdown document")) + "</title>"
                "<style>" + styles + "</style></head><body><main id=main>" + "".join(snapshot.parts) + "</main></body></html>")
    with tempfile.TemporaryDirectory(prefix="markdown-reader-print-") as directory:
        temporary = Path(directory)
        source = temporary / "document.html"
        target = temporary / "document.pdf"
        source.write_text(document)
        command = [executable, "--headless", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                   "--disable-background-networking", "--disable-component-update", "--disable-sync", "--disable-extensions",
                   "--host-resolver-rules=MAP * ~NOTFOUND", "--no-pdf-header-footer", "--virtual-time-budget=1200",
                   "--timeout=15000", "--user-data-dir=" + str(temporary / "profile"),
                   "--print-to-pdf=" + str(target), source.as_uri()]
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=45)
        if result.returncode or not target.is_file():
            raise RuntimeError("The local PDF print process failed. Please retry.")
        pdf = target.read_bytes()
        if not pdf.startswith(b"%PDF"):
            raise RuntimeError("The print process did not return a valid PDF")
        if output.resolve() != resolved_output:
            raise ValueError("The PDF destination changed while exporting")
        if expected is not None:
            if (output.is_symlink() or not output.is_file()
                    or hashlib.sha256(output.read_bytes()).digest() != original_digest):
                raise ValueError("The PDF destination changed while exporting")
            file.replace_contents(pdf, expected, False, Gio.FileCreateFlags.REPLACE_DESTINATION, None)
        else:
            # A newly created destination must not overwrite a file that appeared
            # while the browser was printing. Publish the complete bytes at once.
            descriptor, name = tempfile.mkstemp(prefix=".markdown-reader-", suffix=".pdf", dir=output.parent)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(pdf)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(name, output)
            finally:
                Path(name).unlink(missing_ok=True)
    return output
