import sys

from . import __version__

if "--version" in sys.argv:
    print(f"Markdown Reader {__version__}")
    raise SystemExit(0)

from .runtime import runtime_errors, report_runtime_error

errors = runtime_errors()
if '--check-runtime' in sys.argv:
    print('\n'.join(errors) if errors else 'Markdown Reader native runtime is available.')
    raise SystemExit(bool(errors))
if errors:
    report_runtime_error(errors)
    raise SystemExit(1)

from .app import ReaderApplication

raise SystemExit(ReaderApplication().run(sys.argv))
