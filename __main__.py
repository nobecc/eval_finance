"""Score a normalized task output: python3 -m eval_finance --case case.json."""
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
