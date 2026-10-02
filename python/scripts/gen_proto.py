from __future__ import annotations

import shutil
import sys
import tempfile
from importlib import resources
from pathlib import Path

from grpc_tools import protoc

PYTHON_ROOT = Path(__file__).resolve().parents[1]
PROTO_SOURCE = PYTHON_ROOT.parent / "proto" / "discovery.proto"
PACKAGE_PATH = Path("multihull") / "_proto"
OUTPUT_DIR = PYTHON_ROOT / PACKAGE_PATH


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "__init__.py").touch()
    well_known = str(resources.files("grpc_tools") / "_proto")
    with tempfile.TemporaryDirectory() as scratch:
        staged = Path(scratch) / PACKAGE_PATH / PROTO_SOURCE.name
        staged.parent.mkdir(parents=True)
        shutil.copy(PROTO_SOURCE, staged)
        return protoc.main(
            [
                "protoc",
                f"-I{scratch}",
                f"-I{well_known}",
                f"--python_out={PYTHON_ROOT}",
                f"--pyi_out={PYTHON_ROOT}",
                f"--grpc_python_out={PYTHON_ROOT}",
                str(PACKAGE_PATH / PROTO_SOURCE.name),
            ]
        )


if __name__ == "__main__":
    sys.exit(main())
