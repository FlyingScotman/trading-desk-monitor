#!/usr/bin/env bash
# Fails when a tracked file contains a banned term (organization names, internal report and column names and the
# like: ADR-008, AGENTS.md). The list is NOT in this public repository: it comes from the BANNED_TERMS environment
# variable, which CI fills from the repository secret of the same name. One term per line, matched as a fixed string,
# case-insensitive for Latin letters (add other spellings as separate lines); empty lines and lines starting with #
# are ignored. Without the variable the check is skipped (exit 0): with a ::warning:: on a push to main or a pull
# request from this repository, where the secret is expected; with a plain ::notice:: elsewhere (a fork, Dependabot,
# another branch, a local run). In CI pass PR_HEAD_REPO (the pull request head repository) to tell forks apart.
#
# Output names only file:line, never the matched text, so the public CI log does not reveal the list.
#
# Local run (bash): BANNED_TERMS="$(cat ~/banned_terms.txt)" tools/check_banned_terms.sh
set -euo pipefail

# The secret is expected on a push to main and on a pull request from this repository (not a fork, not Dependabot).
secret_expected() {
  [ "${GITHUB_ACTIONS:-}" = "true" ] || return 1
  [ "${GITHUB_ACTOR:-}" != "dependabot[bot]" ] || return 1
  case "${GITHUB_EVENT_NAME:-}" in
    push) [ "${GITHUB_REF:-}" = "refs/heads/main" ] ;;
    pull_request) [ -n "${PR_HEAD_REPO:-}" ] && [ "${PR_HEAD_REPO}" = "${GITHUB_REPOSITORY:-}" ] ;;
    *) return 1 ;;
  esac
}

skip() {
  if secret_expected; then
    echo "::warning::$1: banned-term check skipped; set the repository secret BANNED_TERMS"
  else
    echo "::notice::$1 (secret not expected here: fork, Dependabot, branch push or local run): banned-term check skipped"
  fi
  exit 0
}

if [ -z "${BANNED_TERMS:-}" ]; then
  skip "BANNED_TERMS is not set"
fi

terms="$(mktemp)"
found="$(mktemp)"
trap 'rm -f "$terms" "$found"' EXIT
printf '%s\n' "$BANNED_TERMS" | tr -d '\r' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
  | { grep -v -e '^$' -e '^#' || true; } > "$terms"

if [ ! -s "$terms" ]; then
  skip "BANNED_TERMS has no terms"
fi

cd "$(git rev-parse --show-toplevel)"
set +e
git grep -n -I -i -F -f "$terms" -- . > "$found"
rc=$?
set -e
# git grep: 0 = matches, 1 = no match, anything else = error
if [ "$rc" -eq 1 ]; then
  echo "banned-term check: $(wc -l < "$terms" | tr -d ' ') terms, no matches in tracked files"
  exit 0
fi
if [ "$rc" -ne 0 ]; then
  echo "::error::banned-term check: git grep failed (exit $rc)"
  exit 2
fi
count=0
while IFS= read -r loc; do
  [ -n "$loc" ] || continue
  echo "::error file=${loc%%:*},line=${loc##*:}::banned term (see the private list in the BANNED_TERMS secret)"
  count=$((count + 1))
done < <(cut -d: -f1,2 "$found")
echo "banned-term check: $count line(s) contain a banned term; replace them with neutral wording or synthetic values"
exit 1
