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

git diff --check

# Keep executable launchers executable; this catches accidental mode changes
# that otherwise make a fresh checkout fail.
for path in cuckoo/test.sh pyunifiwire/test.sh scripts/osprey scripts/osprey-doctor.sh; do
	[ -x "$path" ] || { echo "launcher is not executable: $path" >&2; exit 1; }
done

echo 'repository policy checks passed'
