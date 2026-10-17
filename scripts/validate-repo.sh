#!/bin/sh
# Fast, dependency-free repository policy checks for local work and CI.
set -eu

repo_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_dir"

required='AGENTS.md CONTRIBUTING.md README.md SECURITY.md Makefile docs/README.md docs/quality.md cuckoo/test.sh pyunifiwire/test.sh'
for path in $required; do
	[ -f "$path" ] || { echo "missing required repository file: $path" >&2; exit 1; }
done

# These identifiers belong to private/development environments and must not
# leak into public artifacts. `git grep` skips binary fixtures automatically;
# neutral RFC1918 addresses in protocol tests are intentionally allowed.
if git grep -nEI 'pd[-_ ]?nixos|(^|[^[:alnum:]])finch([^[:alnum:]]|$)' -- . ':(exclude)scripts/validate-repo.sh'; then
	echo 'private or development-only identifiers found in tracked text' >&2
	exit 1
fi

# Validate local Markdown links in the documentation map. This keeps the
# repository navigable for agents after files move or are renamed.
python3 - <<'PY'
import re
from pathlib import Path

root = Path.cwd()
source = root / "docs/README.md"
links = re.findall(r"\[[^]]+\]\(([^)#]+)(?:#[^)]+)?\)", source.read_text(encoding="utf-8"))
missing = []
for link in links:
    if "://" in link or link.startswith("mailto:"):
        continue
    target = (source.parent / link).resolve()
    if not target.is_file():
        missing.append(link)
if missing:
    raise SystemExit("broken links in docs/README.md: " + ", ".join(missing))
PY

# VERSION is the single source of truth for the release version. The sibling
# and distribution metadata that must spell the number out (Home Assistant
# manifest, Homebrew formula, pyunifiwire) are checked here so they cannot
# drift. Release tags are checked against VERSION by .github/workflows/release.yml.
python3 - <<'PY'
import re
from pathlib import Path

root = Path.cwd()
version = (root / "VERSION").read_text(encoding="utf-8").strip()
if not re.fullmatch(r"\d+\.\d+\.\d+", version):
    raise SystemExit(f"VERSION must be MAJOR.MINOR.PATCH, got {version!r}")
checks = {
    "homeassistant/addon/osprey/config.yaml": r'^version: "([^"]+)"$',
    "packaging/homebrew/osprey.rb": r'^  version "([^"]+)"$',
    "pyunifiwire/pyproject.toml": r'^version = "([^"]+)"$',
    "pyunifiwire/src/unifiwire/__init__.py": r'^__version__ = "([^"]+)"$',
}
mismatched = []
for name, pattern in checks.items():
    match = re.search(pattern, (root / name).read_text(encoding="utf-8"), re.MULTILINE)
    found = match.group(1) if match else None
    if found != version:
        mismatched.append(f"{name} ({found!r})")
if mismatched:
    raise SystemExit(f"versions differ from VERSION ({version}): " + ", ".join(mismatched))
PY

git diff --check

# Keep executable launchers executable; this catches accidental mode changes
# that otherwise make a fresh checkout fail.
for path in cuckoo/test.sh pyunifiwire/test.sh scripts/osprey scripts/osprey-doctor.sh; do
	[ -x "$path" ] || { echo "launcher is not executable: $path" >&2; exit 1; }
done

echo 'repository policy checks passed'
