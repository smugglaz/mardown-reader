"""Check native bindings before launching or replacing an installation."""
import importlib
import os
from pathlib import Path
import shutil
import subprocess
import sys

NAMESPACES = (
    ('Gtk', '4.0', 'gir1.2-gtk-4.0'),
    ('Adw', '1', 'gir1.2-adw-1'),
    ('WebKit', '6.0', 'gir1.2-webkit-6.0'),
    ('Soup', '3.0', 'gir1.2-soup-3.0'),
)


def runtime_errors():
    try:
        gi = importlib.import_module('gi')
    except ImportError:
        return ['PyGObject is missing. Install python3-gi.']
    errors = []
    for name, version, package in NAMESPACES:
        try:
            gi.require_version(name, version)
            importlib.import_module('gi.repository.' + name)
        except (ImportError, ValueError) as error:
            errors.append(f'{name} {version} bindings are unavailable. Install {package}. ({error})')
    return errors


def report_runtime_error(errors):
    detail = 'Markdown Reader cannot start.\n\n' + '\n'.join(errors)
    print(detail, file=sys.stderr)
    try:
        folder = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'markdown-reader'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'launch-error.txt').write_text(detail + '\n')
    except OSError:
        pass
    if (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')) and shutil.which('zenity'):
        try:
            subprocess.Popen(['zenity', '--error', '--title=Markdown Reader', '--text=' + detail],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
