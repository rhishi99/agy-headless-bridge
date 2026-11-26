import sys

from .bridge import main as bridge_main


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["serve"] and (len(argv) == 1 or argv[1].startswith("-")):
        from .rest_proxy import main as serve

        return serve(argv[1:])
    return bridge_main(argv)

if __name__ == "__main__":
    raise SystemExit(main())
