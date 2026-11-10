"""Isolated source-tree entry for the bounded real-pair statistical experiment."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from factorlab.strategies.pairs_real_cli import main

if __name__ == "__main__":
    main()
