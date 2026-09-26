#!/usr/bin/env bash
# Guards against a real incident: a commit or tag created in this repo with
# a private email instead of the approved public identity, then pushed.
# Two have happened — a tag's tagger metadata (2026-09-08) and, later,
# seven commits' author/committer fields (2026-09-26, not rewritten; see
# CHANGELOG and the Brain decision record for both). This check exists so a
# third time isn't possible by accident.
#
# Install as a hook (recommended, from the repo root):
#   ln -sf ../../scripts/check-release-identity.sh .git/hooks/pre-commit
#   ln -sf ../../scripts/check-release-identity.sh .git/hooks/pre-push
#
# Or run it by hand before any commit/tag/release work:
#   ./scripts/check-release-identity.sh
set -euo pipefail

APPROVED_EMAIL="76829133+Sako404@users.noreply.github.com"

email="$(git config --local user.email || true)"

if [ -z "$email" ]; then
  echo "REFUSING: no repo-local git user.email set for this clone." >&2
  echo "  A missing repo-local identity silently falls back to your global" >&2
  echo "  config, which is very likely a private address." >&2
  echo "  Fix:  git config --local user.email \"$APPROVED_EMAIL\"" >&2
  exit 1
fi

if [ "$email" != "$APPROVED_EMAIL" ]; then
  echo "REFUSING: repo-local git user.email is '$email', not the approved public identity." >&2
  echo "  Expected: $APPROVED_EMAIL" >&2
  echo "  Fix:      git config --local user.email \"$APPROVED_EMAIL\"" >&2
  echo "  Never fix this by changing your GLOBAL git config — other, private" >&2
  echo "  repositories should keep using your normal address." >&2
  exit 1
fi

echo "check-release-identity: OK ($email)"
