# Label changes

Every change to a ground-truth label in `fixtures/labels.yml`, with the reasoning.

## Why this file exists

The day-3 labels were written from dbt semantics before the agent existed. That ordering
is the only reason the published metrics mean anything — and it is also fragile, because
the tempting move when a metric looks bad is to decide the label was wrong.

So the rule is: **a label changes only when the tool was right and the label was wrong on
dbt semantics, and the argument is written down here.** Not because a number improved.
Anyone reading the eval report can audit every change against its reasoning and judge
whether the relabelling was honest.

If this file is empty, no label has been changed.

## Format

```
### <fixture_id>: <old_severity> -> <new_severity>
**Date:** YYYY-MM-DD (day N)
**Changed by:** who
**What the tool said:** the agent's finding, verbatim
**Why the original label was wrong:** the dbt semantics argument, not the metric
**Rule ids:** any change to expected_rule_ids, with reasoning
```

## Changes

_None yet._

Day 6 is the first session permitted to change a label, and only for fixtures where the
agent's reasoning was correct and the original rationale is wrong on dbt semantics. The
four fixtures flagged as contestable in `fixtures/README.md` (`s07`, `s05`, `s02`, `b10`)
are the likeliest candidates — but "contestable" is not "wrong", and the burden is on the
change.
