from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent
    executable = root / "SHTUCodeProxy" / "SHTUCodeProxy"
    if not executable.exists():
        print(f"Missing bundled executable: {executable}", file=sys.stderr)
        return 1
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
    process = subprocess.Popen([str(executable), *sys.argv[1:]], cwd=str(root))
    return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
