"""agy-headless-bridge — call the Google Antigravity CLI (`agy`) headlessly."""

from importlib.metadata import PackageNotFoundError, version as _version

from .bridge import AgyNotFoundError, AgyTimeoutError, clean, find_agy, resolve_add_dirs, run

try:
    __version__ = _version("agy-headless-bridge")
except PackageNotFoundError:  # running from source without an install
    __version__ = "0.0.0+unknown"

__all__ = [
    "run",
    "find_agy",
    "clean",
    "resolve_add_dirs",
    "AgyNotFoundError",
    "AgyTimeoutError",
    "__version__",
]
