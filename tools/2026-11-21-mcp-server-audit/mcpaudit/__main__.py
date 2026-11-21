"""入口：python -m mcpaudit."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
