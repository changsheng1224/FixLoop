"""Packaged Web assets: Traversable reads work in wheels and zip importers."""

from importlib.resources import files

ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/assets/api.js": ("api.js", "text/javascript; charset=utf-8"),
    "/assets/render.js": ("render.js", "text/javascript; charset=utf-8"),
    "/assets/style.css": ("style.css", "text/css; charset=utf-8"),
}


def read_asset(path):
    """Return bytes without requiring a real filesystem path or extraction."""
    name, mime = ASSETS[path]
    return files(__package__).joinpath("web", name).read_bytes(), mime


def validate_assets():
    """Report a broken installation at startup, before accepting HTTP traffic."""
    for path in ASSETS:
        if not read_asset(path)[0]:
            raise ValueError(f"empty workbench resource: {path}")
