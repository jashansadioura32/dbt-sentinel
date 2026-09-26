# Local review in VS Code

The GitHub App reviews a PR once it exists. Local mode reviews the same change earlier:
after each commit, a git hook runs dbt-sentinel against the branch and opens the report
in VS Code. The analysis is identical, since it's the same CLI, the same manifest
parsing, the same scoring and the same agent. Only the diff source differs.

## Setup

In a shell where you'll keep dbt-sentinel installed:

```bash
git clone https://github.com/jashansadioura32/dbt-sentinel
pip install -e "./dbt-sentinel[agent]"
```

Install from a clone with `-e`. The policy pack lives in the repo's `policies/`
directory and isn't in the wheel, so a non-editable install leaves the agent with no
rules to cite. The review says so when that happens. Point `git config sentinel.policies`
at a pack directory to fix it.

Then, inside your dbt repo:

```bash
sh /path/to/dbt-sentinel/integrations/vscode/install.sh
echo .sentinel/ >> .gitignore
dbt parse                          # writes target/manifest.json; runs no warehouse queries
export OPENAI_API_KEY=...          # optional, see below
```

For the on-demand version, copy `integrations/vscode/tasks.json` into `.vscode/tasks.json`
and run **Tasks: Run Task → dbt-sentinel: review branch**.

## What happens on commit

1. The hook starts `sentinel-review.sh` in the background and returns immediately. The
   commit is never slowed down by the LLM call. Measured: about 300 ms per commit.
2. The script diffs `<base>...HEAD`. That's the merge base, which is exactly what the PR
   will contain.
3. It runs the deterministic review (blast radius, the check layer, Mermaid diagrams) and
   the reviewer agent.
4. It writes `.sentinel/review.md` and opens it with `code --reuse-window`. Press
   `Ctrl+Shift+V` for the rendered preview, including the Mermaid diagrams if you have a
   Mermaid preview extension.

It does nothing in repos that have no `dbt_project.yml`, so it's safe to install
anywhere. Set `SENTINEL_SKIP=1` to skip one commit.

## Configuration

All optional, via `git config` in the dbt repo:

| Key | Default | Use it when |
|---|---|---|
| `sentinel.base` | `origin/HEAD`, then `origin/main`, then `main` | PRs target another branch |
| `sentinel.project` | repo root | the dbt project is in a monorepo subfolder |
| `sentinel.manifest` | `target/manifest.json` | your target path differs |
| `sentinel.policies` | the pack beside the install | you keep your own policy pack |
| `sentinel.command` | `dbt-sentinel` | VS Code's git doesn't see your virtualenv |
| `sentinel.open` | `true` | you'd rather open the report yourself |

The most common setup problem: VS Code's git runs with a PATH that lacks the virtualenv.
The review file then says `dbt-sentinel could not run` and gives the fix:
`git config sentinel.command /path/to/venv/bin/dbt-sentinel` (on Windows,
`.../Scripts/dbt-sentinel.exe`).

## The agent

The hook always passes `--agent`. Without `OPENAI_API_KEY` or the `openai` package, or when
the API fails, the report says the agent was unavailable and why, and keeps the
deterministic analysis. Design rule 5 holds here exactly as it does for the App. A
commit's review costs the same as a PR's; the footer of the agent section prints tokens
and dollars.

## Limitations

- **The manifest is whatever `dbt parse` last wrote.** A model added since then is
  invisible, and reach is under-reported. The review warns when any changed dbt file
  (`.sql`, `.yml`, seeds, docs) was edited after the manifest was generated. The order
  that avoids the warning is edit → `dbt parse` → commit. The check uses file
  modification times, so a `git checkout` that rewrites files also counts as an edit,
  which is right, because the manifest may belong to the other branch.
- **The base ref is only as current as your last `git fetch`.** A stale `origin/main`
  makes the diff include work that has already merged.
- **Windows:** the hook needs Git for Windows' `sh`, which ships with it. The VS Code task
  assumes the default install path `C:\Program Files\Git`.
