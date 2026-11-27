"""Validate workspace attachments and hand the write policy to the host launcher."""

from fnmatch import fnmatchcase
import os
from pathlib import Path
import sys


LAUNCHER_ENV = "AGY_BRIDGE_GUARD_LAUNCHER"


def add_guard_arguments(parser):
    parser.add_argument("--allowed-roots", action="append", metavar="DIR",
                        help="allow writes under this root via the configured guard launcher (repeatable)")
    parser.add_argument("--deny-patterns", action="append", metavar="GLOB",
                        help="deny matching paths and their descendants within allowed roots (repeatable)")


def _directory(value):
    try:
        path = Path(value).resolve(strict=True)
        if path.is_dir():
            return path
    except (OSError, RuntimeError, TypeError, ValueError):
        pass
    raise ValueError(f"guard directory does not exist: {value!r}")


def guarded_command(allowed_roots, deny_patterns, add_dirs=()):
    if allowed_roots is None and deny_patterns is None:
        return (), add_dirs
    if not allowed_roots:
        raise ValueError("the path guard needs at least one --allowed-roots directory")
    roots = [_directory(value) for value in allowed_roots]
    patterns = [".git", ".ssh", *(deny_patterns or [])]
    if any(not isinstance(pattern, str) or not pattern.strip() for pattern in patterns):
        raise ValueError("deny patterns must be non-empty globs")

    dirs = [_directory(value) for value in add_dirs or ()]
    for path in [*roots, *dirs]:
        containing = [root for root in roots if path.is_relative_to(root)]
        if not containing:
            raise ValueError(f"guard directory is outside allowed roots: {path}")
        paths = [item.as_posix() for item in (path, *path.parents)]
        paths += list(path.parts)
        for root in containing:
            relative = path.relative_to(root)
            paths += [item.as_posix() for item in (relative, *relative.parents)]
        for pattern in patterns:
            normalized = os.path.normcase(pattern).replace("\\", "/")
            if any(fnmatchcase(os.path.normcase(candidate).replace("\\", "/"), normalized)
                   for candidate in paths):
                raise ValueError(f"guard directory {path} denied by {pattern!r}")

    launcher = Path(os.environ.get(LAUNCHER_ENV, ""))
    if (not launcher.is_absolute() or not launcher.is_file()
            or (sys.platform == "win32" and launcher.suffix.lower() != ".exe")):
        raise ValueError(
            f"guarded execution requires {LAUNCHER_ENV} to name a verified absolute launcher executable"
        )
    prefix = [str(launcher)]
    for root in roots:
        prefix.extend(("--allowed-roots", str(root)))
    for pattern in patterns:
        prefix.extend(("--deny-patterns", pattern))
    return tuple([*prefix, "--"]), [str(path) for path in dirs]
