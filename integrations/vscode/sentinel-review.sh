#!/bin/sh
# Review the current branch the way its PR will be reviewed, and write the result to
# .sentinel/review.md in the dbt project. Shared by the post-commit hook and the VS Code
# task so the two can never disagree about what "a review" means.
#
# Configuration, all optional, via `git config`:
#   sentinel.base      ref to diff against           (default: origin/HEAD, then origin/main, then main)
#   sentinel.project   dbt project dir, repo-relative (default: repo root)
#   sentinel.manifest  manifest path, project-relative (default: target/manifest.json)
#   sentinel.policies  policy pack directory          (default: the pack beside the install)
#   sentinel.command   how to invoke the CLI          (default: dbt-sentinel)
#   sentinel.open      false to not open the report in VS Code
#
# Set SENTINEL_SKIP=1 to skip a single commit.

[ -n "$SENTINEL_SKIP" ] && exit 0

root=$(git rev-parse --show-toplevel) || exit 0
project="$root/$(git config sentinel.project || echo .)"

# A repo with no dbt project is not an error to report: silence is the right answer for
# every non-dbt repo the hook happens to be installed in.
[ -f "$project/dbt_project.yml" ] || exit 0
cd "$project" || exit 0

base=$(git config sentinel.base)
if [ -z "$base" ]; then
    base=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null)
fi
if [ -z "$base" ]; then
    if git rev-parse --verify --quiet origin/main >/dev/null; then base=origin/main; else base=main; fi
fi

manifest=$(git config sentinel.manifest || echo target/manifest.json)
cmd=$(git config sentinel.command || echo dbt-sentinel)
policies=$(git config sentinel.policies)

# An IDE's git often runs with a PATH that lacks the virtualenv the CLI was installed in.
# Reporting that in the review file beats a hook that fails where no one sees it.
if ! command -v "${cmd%% *}" >/dev/null 2>&1; then
    mkdir -p .sentinel
    printf '# dbt-sentinel could not run\n\n`%s` is not on the PATH git uses. Set the full path with:\n\n    git config sentinel.command "/path/to/venv/bin/dbt-sentinel"\n' "$cmd" > .sentinel/review.md
    exit 0
fi

mkdir -p .sentinel
tmp=.sentinel/review.md.tmp
{
    printf '# dbt-sentinel review\n\n'
    printf '_Branch `%s` vs `%s` · %s · %s_\n\n' \
        "$(git rev-parse --abbrev-ref HEAD)" "$base" \
        "$(git rev-parse --short HEAD)" "$(date '+%Y-%m-%d %H:%M')"
    # Word-splitting of $cmd is deliberate: it allows `python -m dbt_sentinel`.
    # shellcheck disable=SC2086
    $cmd --since "$base" --manifest "$manifest" --agent --mermaid \
        ${policies:+--policies "$policies"} 2>&1
    status=$?
    # Exit 2 means the tool could not run. The error text is already above; say which
    # kind of result this is so a misconfiguration is never read as a clean review.
    if [ "$status" -eq 2 ]; then
        printf '\n> ⚠️ **dbt-sentinel could not run** (exit 2). Fix the error above and commit again, or run the "dbt-sentinel: review branch" task.\n'
    fi
} > "$tmp"
# Renamed into place so VS Code never opens a half-written report.
mv -f "$tmp" .sentinel/review.md

if [ "$(git config --bool sentinel.open)" != "false" ] && command -v code >/dev/null 2>&1; then
    code --reuse-window .sentinel/review.md
fi
