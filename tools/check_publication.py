"""Check tracked/staged files before publication. Never read private directories.

This is a bounded guard, not a guarantee that arbitrary confidential information
can be recognized automatically. Public exports still require human review.
"""
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_ROOTS = {"research_base", "tests", "tools", "examples", "docs", ".github"}
ALLOWED_FILES = {"README.md", "LICENSE", ".gitignore", "pyproject.toml"}
DENIED_PARTS = {"private", "factors-private", "data-private", "runs", "runs-local", "raw", "normalized"}
SECRET = re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9]{24,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")


def check_file(relative, content):
    p = Path(relative)
    issues = []
    if p.is_absolute() or ".." in p.parts or any(part in DENIED_PARTS for part in p.parts):
        issues.append("private/run/vendor-data path")
    if p.parts[0] not in ALLOWED_ROOTS and relative not in ALLOWED_FILES:
        issues.append("outside public allowlist")
    if p.suffix in {".sqlite", ".sqlite3", ".db", ".key", ".pem", ".jpg", ".png", ".xlsx", ".pdf"} or p.name.startswith(".env"):
        issues.append("credential, database or unreviewed binary")
    if len(content) > 2_000_000:
        issues.append("large file requires separate review")
    text = content.decode("utf-8", errors="replace")
    if SECRET.search(text):
        issues.append("credential pattern")
    personal_roots = ("/" + "Users" + "/", "/" + "home" + "/")
    if any(re.search(re.escape(root) + r"[^\s\"']+", text) for root in personal_roots):
        issues.append("local personal path")
    return issues


def main():
    proc = subprocess.run(["git", "ls-files", "-z", "--cached"], cwd=ROOT,
                          capture_output=True, check=True)
    paths = [v.decode() for v in proc.stdout.split(b"\0") if v]
    errors = []
    for name in paths:
        path = ROOT / name
        if path.is_symlink():
            errors.append({"file": name, "issues": ["symlink"]})
            continue
        issues = check_file(name, path.read_bytes())
        if issues:
            errors.append({"file": name, "issues": issues})
    print(json.dumps({"files_checked": len(paths), "passed": bool(paths) and not errors,
                      "errors": errors, "scope": "Tracked-file guard plus manual export review required"}))
    return int(not paths or bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
