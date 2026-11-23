# __main__.py - Interpreter-based entry point, equivalent to the loaf command.
# Used by copyable follow-up commands so they retain the current virtualenv.

# --- Delegate to the same callable registered as the console script ---
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
