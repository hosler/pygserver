"""
pygserver - Entry point for running as a module

Usage:
    python -m pygserver [config_file]
"""

import asyncio
import sys
from pathlib import Path

# Import from parent for development
parent = Path(__file__).parent.parent
if str(parent) not in sys.path:
    sys.path.insert(0, str(parent))

from pygserver.server import run_server


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(run_server(config_path))


if __name__ == "__main__":
    main()
