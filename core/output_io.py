"""Atomic publication for trusted local CLI paths. This is NOT a tenant sandbox."""
import os
import tempfile
import time
from pathlib import Path


def atomic_write(path, data: bytes):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Windows can briefly deny replacement when another publisher has the name open.
        for attempt in range(5):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                if os.name != "nt" or attempt == 4:
                    raise
                time.sleep(0.01 * (2 ** attempt))
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def atomic_write_text(path, text: str):
    atomic_write(path, text.encode("utf-8"))
