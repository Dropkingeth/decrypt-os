#!/usr/bin/env bash
# Create a fresh, single-commit public mirror of a tagged release.
#
#   tools/make_public_repo.sh <tag> <output-dir>
#
# Archives exactly the tree at <tag> (the same bytes `git archive`/the
# release build would ship -- see build/build.sh), commits it as one new
# orphan commit in a brand-new repo at <output-dir>, authored and committed
# by "Decrypt Labs <noreply@decryptlabs.io>" (never a maintainer's local
# git identity) at the current time with a +0000 offset (never the
# maintainer's local timezone), tags that commit <tag> again (also +0000),
# and runs
# tools/privacy_check.py --history on the result, which must pass before
# this script reports success.
#
# This never pushes anywhere and never touches any remote. Pushing
# <output-dir> to the public GitHub repo is a separate, explicit step a
# maintainer does after reviewing the result.
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <tag> <output-dir>" >&2
  exit 2
fi

tag=$1
out=$2
repo=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
identity_name='Decrypt Labs'
identity_email='noreply@decryptlabs.io'

# privacy_check.py's built-in rules do not know the owner's real name, account
# numbers or other private strings -- those are caught only through the
# external DECRYPT_PRIVATE_PATTERNS file. Without it, --history below can
# silently report "clean" while still shipping private data. Fail loudly
# instead of letting a forgotten env var downgrade the gate.
if [[ -z "${DECRYPT_PRIVATE_PATTERNS:-}" ]]; then
  echo "DECRYPT_PRIVATE_PATTERNS is not set or empty." >&2
  echo "The owner's real name, account numbers and other private patterns are only" >&2
  echo "caught through that file; without it, privacy_check.py falls back to its" >&2
  echo "built-in patterns only and can report clean while still leaking private data." >&2
  echo "Set DECRYPT_PRIVATE_PATTERNS to a readable patterns file before running this script." >&2
  exit 2
fi
if [[ ! -f "$DECRYPT_PRIVATE_PATTERNS" || ! -r "$DECRYPT_PRIVATE_PATTERNS" ]]; then
  echo "DECRYPT_PRIVATE_PATTERNS is set but is not a readable file: $DECRYPT_PRIVATE_PATTERNS" >&2
  exit 2
fi

if ! git -C "$repo" rev-parse --verify --quiet "refs/tags/$tag" >/dev/null; then
  echo "no such tag: $tag" >&2
  exit 2
fi
if [[ -e $out ]]; then
  echo "output already exists, refusing to reuse or overwrite it: $out" >&2
  exit 2
fi

mkdir -p "$out"
out=$(cd -- "$out" && pwd -P)
if [[ $out == "$repo" || $out == "$repo"/* ]]; then
  echo "output directory must be outside this repo checkout: $out" >&2
  exit 2
fi

echo "archiving $tag -> $out" >&2
git -C "$repo" archive --format=tar "$tag" | tar -x -C "$out"

git -C "$out" init -q
git -C "$out" config commit.gpgsign false
git -C "$out" config user.name "$identity_name"
git -C "$out" config user.email "$identity_email"
git -C "$out" add -A
# UTC with an explicit +0000 offset: a local offset (e.g. +0200) would publish
# the maintainer's timezone. privacy_check.py --history rejects anything else.
# Date only: today's date at 00:00:00 UTC, so the time of day of a release is not published.
export TZ=UTC
seconds="$(date -u +%s)"
now="$(( seconds - seconds % 86400 )) +0000"
GIT_AUTHOR_NAME="$identity_name" GIT_AUTHOR_EMAIL="$identity_email" GIT_AUTHOR_DATE="$now" \
GIT_COMMITTER_NAME="$identity_name" GIT_COMMITTER_EMAIL="$identity_email" GIT_COMMITTER_DATE="$now" \
  git -C "$out" commit -q -m "Decrypt OS $tag"
GIT_COMMITTER_NAME="$identity_name" GIT_COMMITTER_EMAIL="$identity_email" GIT_COMMITTER_DATE="$now" \
  git -C "$out" tag -a "$tag" -m "Decrypt OS $tag"

echo "running privacy_check.py --history on $out" >&2
python3 "$repo/tools/privacy_check.py" --root "$out" --history

echo "public repo ready: $out (tag $tag, 1 commit, identity $identity_name <$identity_email>)." >&2
echo "Push is a separate, explicit step -- this script never pushes." >&2
