#!/usr/bin/env bash
# Copy the abpipe pipeline into vendor/abpipe/.
#
# The copy is ONE-WAY. ~/work/tts-audiobook is canonical. Refer to
# APP-CONTRACT.md section 3. Never edit a file under vendor/abpipe/.
#
# Only the overlord runs this script.
set -euo pipefail

SRC="${1:-$HOME/work/tts-audiobook-linux-engines}"
DEST="$(cd "$(dirname "$0")/.." && pwd)/vendor/abpipe"

# Two modules are deliberately NOT vendored.
#
#   deliver.py  Stage 8 is the upstream author's delivery stage. It hard-codes a server
#               address, his home directory, and his public domain, and it
#               reads the Audiobookshelf database over SSH. Narratarr has its
#               own target layer and never calls it (APP-CONTRACT 3.1).
#               Leaving it out keeps a stranger's repository free of one
#               person's network, and it turns a rule that a worker could
#               break into one that a worker cannot break. You cannot import
#               a file that is absent.
#
#   cli.py      The command-line front end. Narratarr calls each stage module
#               directly, so it needs no CLI. cli.py also imports deliver.py
#               at the top level, so it could not survive that removal.
#
# Warning: check this list when the pipeline gains a module. A new module
# that imports deliver.py would break the vendored copy at import time.
EXCLUDE=(
  --exclude='__pycache__' --exclude='*.pyc'
  --exclude='deliver.py'  --exclude='cli.py'
)

rsync -a --delete --delete-excluded "${EXCLUDE[@]}" "$SRC/abpipe/" "$DEST/abpipe/"
cp "$SRC/CONTRACT.md" "$DEST/CONTRACT.md"

COMMIT="$(git -C "$SRC" rev-parse HEAD)"
cat > "$DEST/UPSTREAM.txt" <<EOF
upstream: the abpipe pipeline, a private repository
branch:   $(git -C "$SRC" rev-parse --abbrev-ref HEAD)
commit:   $COMMIT
vendored_at: $(date -u +%Y%m%dT%H%M%SZ)

rule: one-way. Upstream is canonical. Never edit a file under vendor/abpipe/.
not vendored: deliver.py, cli.py. Refer to tools/vendor-abpipe.sh.
EOF

echo "vendored abpipe at $COMMIT"
python3 - "$DEST" <<'PY'
import ast, pathlib, sys
root = pathlib.Path(sys.argv[1]) / "abpipe"
bad = []
for path in sorted(root.rglob("*.py")):
    tree = ast.parse(path.read_text(), str(path))
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("abpipe"):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.Import):
            names = [a.name.split(".")[-1] for a in node.names if a.name.startswith("abpipe")]
        if {"deliver", "cli"} & set(names):
            bad.append(f"{path.relative_to(root.parent)}:{node.lineno}")
if bad:
    print("FAIL: a vendored module imports an excluded module:", *bad, sep="\n  ")
    sys.exit(1)
print("check: no vendored module imports deliver or cli")
PY
