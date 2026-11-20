"""heliopuddle: a simple multi-location datastore manager built on Prefect."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("heliopuddle")
except PackageNotFoundError:  # pragma: no cover - only when not installed
    __version__ = "0.0.0"
