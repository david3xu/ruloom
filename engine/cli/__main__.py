"""Allow ``python -m engine.cli`` to invoke the ``ruloom`` entry point."""

from . import main

if __name__ == "__main__":
    raise SystemExit(main())
