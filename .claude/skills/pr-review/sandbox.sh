#!/usr/bin/env bash
# Check out a PR's head into an isolated git worktree and run the same gates CI
# runs, so the reviewer judges the code by what it does, not only by the diff.
# Never pushes: the worktree is detached and deleted by `sandbox.sh clean`.
#
#   sandbox.sh <pr-number>          set up + run checks, write report
#   sandbox.sh clean <pr-number>    remove the worktree
set -uo pipefail

repo_root="$(git rev-parse --show-toplevel)"
sandbox_root="${PR_SANDBOX_DIR:-${TMPDIR:-/tmp}/ontos-pr-sandbox}"

if [[ "${1:-}" == "clean" ]]; then
  pr="${2:?usage: sandbox.sh clean <pr-number>}"
  git -C "$repo_root" worktree remove --force "$sandbox_root/pr-$pr" 2>/dev/null || true
  git -C "$repo_root" branch -D "pr-review/$pr" 2>/dev/null || true
  echo "removed $sandbox_root/pr-$pr"
  exit 0
fi

pr="${1:?usage: sandbox.sh <pr-number>}"
base="${2:-main}"
wt="$sandbox_root/pr-$pr"
report="$sandbox_root/pr-$pr.report.md"
mkdir -p "$sandbox_root"

git -C "$repo_root" fetch --quiet origin "$base" "+pull/$pr/head:pr-review/$pr" || {
  echo "could not fetch PR #$pr (pull/$pr/head)"; exit 2; }
git -C "$repo_root" worktree remove --force "$wt" 2>/dev/null || true
git -C "$repo_root" worktree add --quiet --detach "$wt" "pr-review/$pr"

cd "$wt"
head_sha="$(git rev-parse HEAD)"
merge_base="$(git merge-base HEAD "origin/$base")"
mapfile -t changed_py < <(git diff --name-only --diff-filter=ACMR "$merge_base" HEAD -- '*.py')

status=0
{
  echo "# Sandbox report — PR #$pr"
  echo
  echo "- head: \`$head_sha\`"
  echo "- merge-base with \`$base\`: \`$merge_base\`"
  echo "- changed python files: ${#changed_py[@]}"
  echo
} > "$report"

run() {
  local name="$1"; shift
  local out rc
  out="$("$@" 2>&1)"; rc=$?
  [[ $rc -ne 0 ]] && status=1
  {
    echo "## $name — $([[ $rc -eq 0 ]] && echo PASS || echo "FAIL (exit $rc)")"
    echo
    echo '```'
    echo "$out" | tail -n 80
    echo '```'
    echo
  } >> "$report"
}

run "uv sync" uv sync --extra dev --frozen --quiet
run "ruff check" uv run ruff check ontos tests
run "ruff format --check (changed files)" \
  bash -c '[[ $# -eq 0 ]] && echo "no python changes" || uv run ruff format --check "$@"' _ "${changed_py[@]}"
run "mypy --strict" uv run mypy ontos
run "pytest compliance (gate-blocking)" uv run pytest -q tests/compliance
run "pytest unit" uv run pytest -q -m "not integration and not benchmark" --ignore=tests/compliance

echo "overall: $([[ $status -eq 0 ]] && echo PASS || echo FAIL)" >> "$report"
echo "worktree: $wt"
echo "report:   $report"
exit $status
