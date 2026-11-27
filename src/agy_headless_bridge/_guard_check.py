"""Run a disposable filesystem check against the configured guard launcher."""

from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event

from ._guard import guarded_command
from .bridge import AgyTimeoutError, _probe


_CHILD = """
import errno
from pathlib import Path
import sys

allowed, *blocked = map(Path, sys.argv[1:])
allowed.write_bytes(b'checked')
unexpected = False
for path in blocked:
    try:
        path.write_bytes(b'unexpected')
    except OSError as exc:
        if exc.errno not in (errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOENT):
            raise
    else:
        unexpected = True
print('AGY_GUARD_CHECK', flush=True)
sys.exit(1 if unexpected else 73)
"""


def main():
    with tempfile.TemporaryDirectory(prefix="agy-guard-check-") as folder:
        base = Path(folder).resolve()
        root = base / "workspace"
        root.mkdir()
        allowed = root / "allowed.txt"
        protected = [root / ".git" / "existing.txt", root / ".ssh" / "existing.txt",
                     root / "blocked-existing.txt"]
        missing = [root / "blocked-new.txt", base / "workspace-outside" / "outside.txt"]
        for path in protected + missing:
            path.parent.mkdir(parents=True, exist_ok=True)
        for path in protected:
            path.write_bytes(b"original")

        child = [sys.executable, "-I", "-S", "-c", _CHILD, str(allowed),
                 *(str(path) for path in protected + missing)]
        # Launch the writer as a descendant and relay its stdio and exit status.
        parent = [sys.executable, "-I", "-S", "-c",
                  "import subprocess, sys; sys.exit(subprocess.call(sys.argv[1:]))", *child]
        try:
            prefix, _ = guarded_command([str(root)], ["blocked-*.txt"])
            out, err, status = _probe([*prefix, *parent], 15, Event())
        except (AgyTimeoutError, OSError, ValueError) as exc:
            print(f"Guard launcher check failed: {exc}", file=sys.stderr)
            return 1

        failures = []
        if not allowed.is_file() or allowed.read_bytes() != b"checked":
            failures.append("allowed.txt was not written")
        failures.extend(f"{path.relative_to(base)} changed" for path in protected
                        if not path.is_file() or path.read_bytes() != b"original")
        failures.extend(f"{path.relative_to(base)} was created" for path in missing
                        if path.exists() or path.is_symlink())
        completed = "AGY_GUARD_CHECK" in (out + "\n" + err).splitlines()
        if completed and status == 1:
            failures.append("the writer reported an allowed prohibited write")
        elif not completed or status != 73:
            failures.append(f"launcher did not preserve probe output and exit status (got {status})")
        if failures:
            print("Guard launcher check failed.\n" + "\n".join(failures), file=sys.stderr)
            return 1

    print("Guard launcher passed the disposable descendant write check.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
