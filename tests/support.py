"""Test workspace with inherited Windows ACLs, including sandbox access."""

from contextlib import contextmanager
from pathlib import Path
import shutil
import uuid


@contextmanager
def temp_directory():
    boundary = (Path(__file__).resolve().parents[1] / "data" / "test-work").resolve()
    boundary.mkdir(parents=True, exist_ok=True)
    path = boundary / uuid.uuid4().hex
    path.mkdir()
    try:
        yield str(path)
    finally:
        resolved = path.resolve()
        if resolved.parent != boundary or resolved == boundary:
            raise RuntimeError("Test cleanup target escaped the test workspace")
        shutil.rmtree(resolved)
