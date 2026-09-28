# Contributing

Thanks for looking. dbt-sentinel optimises for correctness that can be proven, so the bar
for a change is evidence, not just a passing diff.

## Setup

```bash
git clone https://github.com/jashansadioura32/dbt-sentinel.git
cd dbt-sentinel
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m pytest tests/ -q
```

## Before you open a PR

Run what CI runs:

```bash
python -m pytest tests/ -q
python -m evals.runner && python -m evals.retrieval_eval && python -m evals.checks_eval
python -m evals.sql_policy_eval --dry-run
python .github/scripts/check_baselines.py
```

`check_baselines.py` fails if any published metric regresses. Moving a floor is allowed
only when the published doc that states it changes in the same commit.

## The rules

1. **Every bug becomes a regression test.** Name the failure it prevents in the test's
   docstring.
2. **Labels before code.** A new check or eval fixture is specified and labelled before
   it's implemented, so the eval measures the code rather than mirroring it. A disputed
   label is argued in [evals/LABEL_CHANGES.md](evals/LABEL_CHANGES.md), never silently
   edited.
3. **Deterministic first.** If graph traversal or string parsing can answer a question,
   don't ask the LLM.
4. **Reach amplifies; it doesn't trigger.** Severity comes from a structural change only.
5. **No new dependency without a reason** in the PR description. No LangChain,
   LlamaIndex, CrewAI or `networkx`.
6. **Comments explain why, not what,** and name the failure mode a non-obvious decision
   prevents.

The design and its rejected alternatives are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
