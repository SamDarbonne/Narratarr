#!/usr/bin/env bash
# Build the public snapshot of Narratarr, in a clean directory.
#
# Warning: the public repository gets a SINGLE fresh commit, and none of this
# repository's history. That is deliberate, and it is the same rule that
# governs the vendored pipeline.
#
# Two reasons:
#   1. PROGRESS.md is the private run log. Every revision of it holds the
#      operator's LAN address, his public domain, and his name. Redacting the
#      current file does nothing about the 20 revisions behind it.
#   2. One early commit briefly held a live API key. The key is rotated and
#      dead, but a dead key in a public history still invites a reader to try
#      it.
#
# Publishing a snapshot is honest. The development history is a private
# working record, not a deliverable.
#
# Usage:  tools/prepare-public.sh [DEST]
# Then review DEST by hand, and only then create a remote and push.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:-/tmp/narratarr-public}"

# Files that stay private. Each one is an internal run artifact, not product.
#   PROGRESS.md   the run log: the operator's machines, his decisions, his name
#   feature.md    a proof document, written for the operator's dashboard
#   tests.md      the same
#   proof/        the proof harness: his LAN address and his proofkit path
#   proof.config.json
#   docs/screenshots/  captured against his instance
PRIVATE=(
  PROGRESS.md
  feature.md
  tests.md
  state.md
  proof
  proof.config.json
)

rm -rf "$DEST"
mkdir -p "$DEST"

# Copy the tracked tree only. An untracked file is never published by
# accident, and .gitignore already keeps the key fixture and the scratch
# output out of the tree.
git -C "$SRC" archive HEAD | tar -x -C "$DEST"

for p in "${PRIVATE[@]}"; do
  rm -rf "${DEST:?}/$p"
done

echo "== the public snapshot is at $DEST =="
echo
echo "-- checking for anything identifying --"
# Warning: match a surname on its own too, not only the punctuated form.
# The first version of this list missed `the-informer-oflaherty.epub`,
# because it looked for "O'Flaherty" with the apostrophe and the capital.
# That string sat in an executable default, which no comment-level pass
# would ever have found.
#
# `samdarbonne.com` is the private domain and must never appear. The bare
# GitHub handle is exempt: the repository is published under that account,
# so `ghcr.io/samdarbonne/narratarr` is the real, intended image name.
PATTERN='samdarbonne\.com|jkprod|192\.168\.|/home/samd|/Users/samd|\bSam\b'
PATTERN="$PATTERN"'|oflaherty|the-informer|paris-trout|pioneer-urbanites|americas-first'
PATTERN="$PATTERN"'|Informer|Paris Trout|Gypo|Gippo|Seagraves|McPhillip'
# --exclude keeps this script from matching the pattern it defines.
if grep -rInE "$PATTERN" "$DEST" --exclude='prepare-public.sh' 2>/dev/null; then
  echo
  echo "FAIL: the snapshot still holds an identifying string. Fix it before you push."
  exit 1
fi
echo "clean: no address, no domain, no home directory, no name."
echo
echo "-- checking for a secret --"
# The value must be a QUOTED literal of some length. An earlier version
# matched `compound_token = _containing_compound(occ)`, because the
# right-hand side is a long identifier, and a check that cries wolf gets
# switched off.
#
# An obvious placeholder is allowed through. A test that proves a token
# never leaks has to name a token to prove it with, and refusing that test
# would delete the test rather than the risk.
SECRET='(api[_-]?key|token|secret|password)["'"'"']?[[:space:]]*[:=][[:space:]]*["'"'"'][A-Za-z0-9_./-]{16,}["'"'"']'
PLACEHOLDER='secret-value|fake|dummy|example|placeholder|changeme|your-|xxxx|REPLACE|<.*>'
if grep -rInE "$SECRET" \
     "$DEST" --include='*.py' --include='*.ts' --include='*.tsx' --include='*.mjs' \
     --include='*.json' --include='*.yml' --include='*.example' 2>/dev/null \
     | grep -vIE "$PLACEHOLDER"; then
  echo "FAIL: a possible secret. Check it before you push."
  exit 1
fi
echo "clean: no secret."
echo
echo "-- checking the acquisition-free rule --"
if grep -rInE '\b(torrent|nzb|usenet|jackett|prowlarr|qbittorrent|magnet:)\b' \
     "$DEST" --include='*.py' --include='*.ts' --include='*.tsx' 2>/dev/null; then
  echo "FAIL: acquisition code. Refer to APP-CONTRACT section 1.1."
  exit 1
fi
echo "clean: no acquisition code."
echo
echo "Next, by hand:"
echo "  cd $DEST && git init -b main && git add -A"
echo "  git commit -m 'Narratarr v1'"
echo "  # then create the remote and push. Not before the operator says so."
