"""Check Git-selected files without opening dotenv files or displaying matched secrets."""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import re
import subprocess
from pathlib import Path

RULES = {
    "private_key": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    "provider_token": r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}",
    "github_token": r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})",
    "aws_access_id": r"\bAKIA[A-Z0-9]{16}\b",
    "literal_api_setting": (
        r"(?:OPENAI_API_KEY|SEMANTIC_SCHOLAR_API_KEY|S2_API_KEY)"
        r"[\"']?\s*[:=]\s*[\"'][A-Za-z0-9_-]{24,}[\"']"
    ),
}


def forbidden_path(name: str) -> bool:
    path = Path(name)
    return (any(p == ".env" or p.startswith(".env.") for p in path.parts)
            or any(p in {".work", ".venv", "node_modules", "__pycache__"} for p in path.parts)
            or path.suffix in {".db", ".sqlite", ".sqlite3", ".pdf", ".log"}
            or name.startswith("evaluation/reports/"))


def scan(data: bytes, *, compressed: bool = False) -> set[str]:
    if compressed:
        data = gzip.decompress(data)
    text = data.decode("utf-8", errors="ignore")
    found = {rule for rule, pattern in RULES.items() if re.search(pattern, text)}
    # The regression bundle includes a small allowlisted map of base64-encoded
    # cache files. Check decoded content too; encoding must not hide a secret.
    try:
        document = json.loads(text)
        if isinstance(document, dict) and document.get("format") == "recorded-regression-v1":
            for encoded in document["files"].values():
                found.update(scan(base64.b64decode(encoded, validate=True)))
    except (ValueError, KeyError, TypeError):
        pass
    return found


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args])


def check(*, staged: bool = False, history: bool = False) -> dict:
    command = ("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z") if staged else (
        "ls-files", "-z",
    )
    paths = [p for p in git(*command).decode().split("\0") if p]
    findings = []
    for name in paths:
        if forbidden_path(name):
            findings.append({"path": name, "rule": "excluded_publication_path"})
            continue  # In particular, never read .env from worktree or index.
        data = git("show", f":{name}")
        findings.extend({"path": name, "rule": rule}
                        for rule in sorted(scan(data, compressed=name.endswith(".gz"))))
    empty_dotenv_history = True
    if history:
        dotenv_objects = set()
        for line in git("log", "--all", "--format=", "--raw", "--no-abbrev", "--",
                        ".env", ".env.*", "**/.env", "**/.env.*").decode().splitlines():
            if line.startswith(":"):
                for oid in line.split()[2:4]:
                    if set(oid) != {"0"}:
                        dotenv_objects.add(oid)
        objects = {}
        for line in git("rev-list", "--objects", "--all").decode().splitlines():
            oid, _, name = line.partition(" ")
            if name:
                objects[oid] = name
        metadata = subprocess.check_output(
            ["git", "cat-file", "--batch-check"], input="\n".join(objects).encode(),
        ).decode().splitlines()
        for item in metadata:
            oid, kind, size = item.split()
            if kind != "blob":
                continue
            name = objects[oid]
            if oid in dotenv_objects:
                if size != "0":
                    empty_dotenv_history = False
                    findings.append({"path": name, "rule": "nonempty_dotenv_history"})
                continue  # Inspect blob metadata only, never dotenv contents.
            if size == "0":
                continue
            findings.extend({"path": name, "rule": f"history:{rule}"}
                            for rule in sorted(scan(git("cat-file", "blob", oid),
                                                    compressed=name.endswith(".gz"))))
    return {"status": "passed" if not findings else "failed", "files_checked": len(paths),
            "dotenv_contents_read": False, "history_checked": history,
            "historical_dotenv_blobs_empty": empty_dotenv_history, "findings": findings}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--tracked", action="store_true")
    group.add_argument("--staged", action="store_true")
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args()
    result = check(staged=args.staged, history=args.history)
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["status"] == "passed" else 1)
