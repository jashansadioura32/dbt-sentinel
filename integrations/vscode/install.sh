#!/bin/sh
# Install the dbt-sentinel post-commit hook into the git repo you run this from:
#
#   sh /path/to/dbt-sentinel/integrations/vscode/install.sh
#
# It never overwrites an existing post-commit hook. Clobbering someone's hook would
# silently switch off whatever it did.
set -e

here=$(cd "$(dirname "$0")" && pwd)
hooks=$(git rev-parse --git-path hooks)
mkdir -p "$hooks"

cp "$here/sentinel-review.sh" "$hooks/sentinel-review.sh"

if [ -f "$hooks/post-commit" ] && ! grep -q "sentinel-review.sh" "$hooks/post-commit"; then
    echo "A post-commit hook already exists at $hooks/post-commit, so it was left alone."
    echo "To chain dbt-sentinel, add this line to the end of it:"
    echo
    echo "    sh \"\$(dirname \"\$0\")/sentinel-review.sh\" >/dev/null 2>&1 </dev/null &"
    exit 1
fi
cp "$here/post-commit" "$hooks/post-commit"
chmod +x "$hooks/post-commit" "$hooks/sentinel-review.sh"

echo "Installed. The next commit writes .sentinel/review.md and opens it in VS Code."
echo
echo "Next steps:"
echo "  1. echo .sentinel/ >> .gitignore"
echo "  2. dbt parse                      # the review reads target/manifest.json"
echo "  3. export OPENAI_API_KEY=...      # optional; without it the agent section says so"
echo "  4. git config sentinel.base origin/main   # if your PRs target another branch"
echo
echo "For the on-demand task, copy $here/tasks.json into .vscode/tasks.json."
