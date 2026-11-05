"""agy-headless-bridge — call the Google Antigravity CLI (`agy`) headlessly."""

from importlib.metadata import PackageNotFoundError, version as _version

from .bridge import (
    AgyResult,
    AgyExitError,
    AgyNotFoundError,
    AgyQuotaError,
    AgyTimeoutError,
    clean,
    find_agy,
    resolve_add_dirs,
    run,
    run_result,
)

try:
    __version__ = _version("agy-headless-bridge")
except PackageNotFoundError:  # running from source without an install
    __version__ = "0.0.0+unknown"

__all__ = [
    "run",
    "run_result",
    "AgyResult",
    "find_agy",
    "clean",
    "resolve_add_dirs",
    "AgyNotFoundError",
    "AgyTimeoutError",
    "AgyExitError",
    "AgyQuotaError",
    "__version__",
]
