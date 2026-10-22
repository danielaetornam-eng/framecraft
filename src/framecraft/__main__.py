"""``python -m framecraft`` is the same as ``fc-trace``."""

from framecraft.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
