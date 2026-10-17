"""Compatibility shim: `PYTHONPATH=. python3 tools/bryton_agent.py` still works from a repo checkout."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bryton.agent import main  # noqa: E402

if __name__ == "__main__":
    main()
