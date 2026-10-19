"""Small immutable-output, hashing and append-only ledger helpers."""
from pathlib import Path
import hashlib
import json
import os
import time


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def save_json(path, value):
    """Never replace an existing scientific record or manifest."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def load_json(path):
    return json.loads(Path(path).read_text())


def append_event(session, kind, **fields):
    path = Path(session) / "audit_ledger.jsonl"
    event = {"kind": kind, "timestamp_ns": time.time_ns(), **fields}
    with path.open("a") as stream:
        stream.write(canonical(event).decode() + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return event


def ledger(session):
    path = Path(session) / "audit_ledger.jsonl"
    if not path.exists():
        return []
    # A truncated final line is not silently discarded on resume.
    return [json.loads(line) for line in path.read_text().splitlines()]


def stat_identity(path):
    s = Path(path).stat()
    return {"size": s.st_size, "mtime_ns": s.st_mtime_ns, "ctime_ns": s.st_ctime_ns,
            "inode": s.st_ino, "device": s.st_dev}
