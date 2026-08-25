"""Convenience launcher. Default invocation cannot start a simulation."""

from pathlib import Path
import sys

# Keep the specified Conda environment untouched: this launcher imports the local
# source tree directly, so an editable installation is not required.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pybamm_w10.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
