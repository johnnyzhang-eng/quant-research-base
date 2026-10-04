import fcntl
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path


class ContractError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def load_json(path):
    def reject(value):
        raise ContractError(f"Non-finite JSON number: {value}")
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ContractError(f"Duplicate JSON key: {key}")
            out[key] = value
        return out
    return json.loads(Path(path).read_text(), parse_constant=reject, object_pairs_hook=unique)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation: a previous evidence file is never silently replaced.
    with path.open("x") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def inside(root, relative):
    root = Path(root).resolve()
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ContractError(f"Path must be relative without traversal: {relative}")
    target = root / path
    # Refuse symlinks, including intermediate directory links.
    cursor = root
    for part in path.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ContractError(f"Symlink not accepted: {relative}")
    if not target.resolve().is_relative_to(root):
        raise ContractError(f"Path escapes root: {relative}")
    return target


def append_event(registry, event):
    registry = Path(registry)
    registry.parent.mkdir(parents=True, exist_ok=True)
    with registry.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def seal_run(run):
    run = Path(run)
    files = {}
    for path in sorted(run.rglob("*")):
        if path.is_symlink():
            raise ContractError("Evidence bundle contains a symlink")
        if path.is_file():
            files[str(path.relative_to(run))] = digest(path)
    write_json(run / "seal.json", {"schema_version": "1", "files": files,
                                  "scope": "local change detection; not external notarization"})
    return digest(run / "seal.json")


def verify_run(run, registry):
    run = Path(run).resolve()
    seal = load_json(run / "seal.json")
    expected = seal["files"]
    actual = {str(p.relative_to(run)) for p in run.rglob("*") if p.is_file()}
    errors = []
    if any(p.is_symlink() for p in run.rglob("*")):
        errors.append("symlink in evidence")
    if actual != set(expected) | {"seal.json"}:
        errors.append("added or removed files")
    for name, checksum in expected.items():
        path = inside(run, name)
        if not path.is_file() or digest(path) != checksum:
            errors.append(f"content changed: {name}")
    events = [load for line in Path(registry).read_text().splitlines()
              if (load := json.loads(line)).get("run_id") == run.name]
    terminals = [e for e in events if e.get("event") == "FINISHED"]
    if len(terminals) != 1 or terminals[0].get("seal_sha256") != digest(run / "seal.json"):
        errors.append("registry terminal/seal mismatch")
    return {"run_id": run.name, "verified": not errors, "errors": errors,
            "files_checked": len(expected)}
