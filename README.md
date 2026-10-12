# Markdown Reader

A native Linux application for reading and editing Markdown, built with Python,
GTK 4, libadwaita, and WebKitGTK 6.

## Run

Use Python 3.11 or newer with PyGObject and the GTK 4, libadwaita, WebKitGTK 6,
and Soup 3 introspection bindings. On Ubuntu, the relevant packages are
`python3-gi`, `gir1.2-gtk-4.0`, `gir1.2-adw-1`, `gir1.2-webkit-6.0`, and
`gir1.2-soup-3.0`.

From the repository directory:

```sh
/usr/bin/python3 -m markdown_reader --check-runtime
/usr/bin/python3 -m markdown_reader
```

The repository includes the Python application and a bundled frontend in
`frontend/dist`. The original frontend source and build configuration are not
included. No frontend build is needed to run this distribution.

## Install

```sh
/usr/bin/python3 scripts/manage_install.py install
```

This installs the application for the current user. To make it the default
Markdown application:

```sh
/usr/bin/python3 scripts/manage_install.py set-default
```

To uninstall:

```sh
markdown-reader-uninstall
```

Writing Tools require a separately installed, signed-in Codex version supported
by the application.

## License

MIT. Third-party frontend licenses are in
`frontend/dist/THIRD_PARTY_NOTICES.txt`.
