#!/usr/bin/env python3
"""Check distributable files and local Markdown links without network access."""
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ["README.md", "README.en.md", "LICENSE", "CONTRIBUTING.md", "SECURITY.md",
            "CODE_OF_CONDUCT.md", "CHANGELOG.md", ".github/workflows/ci.yml"]


def main():
    errors = []
    for name in REQUIRED:
        if not (ROOT / name).is_file():
            errors.append(f"Missing required file: {name}")
    result = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                            cwd=ROOT, check=True, capture_output=True)
    names = sorted(set(result.stdout.decode().strip("\0").split("\0")) - {""})
    for name in names:
        path = ROOT / name
        if any(part in {".venv", "__pycache__", "runtime", "attachments"} for part in path.parts):
            errors.append(f"Local runtime data must not be distributed: {name}")
            continue
        if path.suffix in {".log", ".backup", ".pyc"} or path.name.startswith(".env"):
            errors.append(f"Local configuration/diagnostic file: {name}")
        if not path.is_file() or path.suffix in {".png", ".gif", ".jpg"}:
            continue
        text = path.read_text(encoding="utf-8")
        if not text.endswith("\n"):
            errors.append(f"Missing final newline: {name}")
        # Real home paths and copied task identifiers belong in local diagnostics,
        # not public examples. Tests use clearly synthetic UUIDs.
        if re.search(r"/Users/[A-Za-z0-9_.-]+/", text):
            errors.append(f"Personal absolute home path: {name}")
        if re.search(r"\b01[a-f0-9]{6}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\b", text):
            errors.append(f"Non-synthetic task identifier: {name}")
        if re.search(r"\b(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]{20,}", text):
            errors.append(f"Possible credential: {name}")
        if path.suffix == ".md":
            for target in re.findall(r"\[[^\]]*\]\(([^\s)]+)\)", text):
                parsed = urlsplit(target)
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                linked = (path.parent / unquote(parsed.path)).resolve()
                if not linked.exists():
                    errors.append(f"Broken local link in {name}: {target}")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"Repository checks passed ({len(names)} files).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
