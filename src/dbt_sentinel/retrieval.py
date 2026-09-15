"""Hybrid retrieval over the policy pack.

Two stages, in order:

1. **Structural prefilter** — `applies_to` is matched deterministically against the
   changed node's kind, file path and config. A rule that cannot apply is eliminated
   before any scoring happens; this is a glob and a dict lookup, not a similarity
   question, so design rule 1 says answer it structurally.
2. **Lexical similarity** — the surviving rules are ranked by TF-IDF cosine against a
   summary of the change.

Only the rule pack is vectorised. The manifest, the lineage graph and the SQL are never
embedded: reach is a BFS, file resolution is a string match, and both are exact. Turning
an exact question into a nearest-neighbour question loses precision and costs money.

On "embeddings": this is lexical similarity over a bag of stemmed tokens, not a semantic
embedding. It is called out plainly rather than dressed up, because the failure mode is
specific and worth documenting — it matches paraphrases only when they share vocabulary.
A rule about "personally identifiable information" will not match a diff that says
"email" unless the rule also lists `email` as a keyword, which is exactly why every rule
carries an explicit `keywords` list. The tradeoff bought a zero-dependency,
fully-offline, deterministic retriever, so eval numbers are reproducible by anyone who
clones the repo without an API key.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

import yaml

from .models import ChangedNode, ChangeType, NodeKind

POLICY_DIR = Path(__file__).resolve().parents[2] / "policies"
CACHE_DIR = Path(".sentinel_cache")
CACHE_FILE = "policy_vectors.json"

# Cosine floor for citing a rule that matched no curated keyword. Tuned on the day-4
# retrieval eval: below this, shared incidental vocabulary ("model", "column", "select")
# is enough to surface an unrelated rule.
_MIN_SCORE = 0.15

# Tokens that appear in nearly every rule and nearly every diff carry no signal, and
# leaving them in lets "the change of a model" out-rank a genuine keyword hit.
_STOPWORDS = frozenset(
    """
    a an the and or but if then than that this these those is are was were be been being
    do does did to of in on at by for with from as it its into no not any all every some
    which who whom whose when where how why what while so such only own same too very can
    will just should now
    """.split()
)

_TOKEN = re.compile(r"[a-z_][a-z0-9_]*")


@dataclass(frozen=True)
class Rule:
    rule_id: str
    title: str
    severity: str
    description: str
    guidance: str
    example_violation: str
    keywords: tuple[str, ...] = ()
    node_kinds: tuple[str, ...] = ()
    path_globs: tuple[str, ...] = ()
    requires: tuple[tuple[str, object], ...] = ()
    source_file: str = ""

    @property
    def searchable_text(self) -> str:
        """Keywords are repeated because they are curated signal, not prose.

        Without the repetition a 120-word description drowns the five words that actually
        identify when the rule applies.
        """
        keywords = " ".join(self.keywords)
        return " ".join(
            [self.title, keywords, keywords, keywords, self.description, self.example_violation]
        )


@dataclass
class RetrievedRule:
    rule: Rule
    score: float
    matched_keywords: tuple[str, ...] = ()
    prefilter_only: bool = False

    @property
    def rule_id(self) -> str:
        return self.rule.rule_id


@dataclass
class RetrievalResult:
    """Kept separate from the ranked list so `--explain` can show what was eliminated."""

    retrieved: list[RetrievedRule] = field(default_factory=list)
    eliminated: list[tuple[str, str]] = field(default_factory=list)  # (rule_id, reason)
    query_terms: tuple[str, ...] = ()


def _tokenize(text: str) -> list[str]:
    """Split identifiers so `customer_id` also contributes `customer` and `id`."""
    tokens: list[str] = []
    for raw in _TOKEN.findall(text.lower()):
        if raw in _STOPWORDS:
            continue
        tokens.append(raw)
        if "_" in raw:
            tokens.extend(p for p in raw.split("_") if p and p not in _STOPWORDS)
    return tokens


class PolicyPack:
    def __init__(self, rules: list[Rule]):
        self._rules = rules
        self._by_id = {r.rule_id: r for r in rules}
        self._idf: dict[str, float] = {}
        self._vectors: dict[str, dict[str, float]] = {}

    # ---------- loading ----------

    @classmethod
    def load(cls, policy_dir: str | Path | None = None) -> PolicyPack:
        directory = Path(policy_dir or POLICY_DIR)
        if not directory.exists():
            raise FileNotFoundError(
                f"policy pack not found at {directory}. Expected *.yml rule files there."
            )

        rules: list[Rule] = []
        for path in sorted(directory.glob("*.yml")):
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for raw in payload.get("rules") or []:
                applies = raw.get("applies_to") or {}
                requires = applies.get("requires") or {}
                rules.append(
                    Rule(
                        rule_id=raw["rule_id"],
                        title=raw.get("title", ""),
                        severity=raw.get("severity", "medium"),
                        description=(raw.get("description") or "").strip(),
                        guidance=(raw.get("guidance") or "").strip(),
                        example_violation=(raw.get("example_violation") or "").strip(),
                        keywords=tuple(raw.get("keywords") or ()),
                        node_kinds=tuple(applies.get("node_kinds") or ()),
                        path_globs=tuple(applies.get("path_globs") or ()),
                        requires=tuple(sorted(requires.items())),
                        source_file=path.name,
                    )
                )

        duplicates = [rid for rid, n in Counter(r.rule_id for r in rules).items() if n > 1]
        if duplicates:
            raise ValueError(
                f"duplicate rule_id(s) in the policy pack: {', '.join(sorted(duplicates))}. "
                f"Rule ids are referenced by the eval labels and must be unique."
            )

        pack = cls(rules)
        pack._build_vectors()
        return pack

    def __len__(self) -> int:
        return len(self._rules)

    @property
    def rules(self) -> list[Rule]:
        return list(self._rules)

    def get(self, rule_id: str) -> Rule | None:
        return self._by_id.get(rule_id)

    # ---------- vectorising ----------

    def _build_vectors(self) -> None:
        docs = {r.rule_id: _tokenize(r.searchable_text) for r in self._rules}
        n_docs = len(docs) or 1

        doc_freq: Counter[str] = Counter()
        for tokens in docs.values():
            doc_freq.update(set(tokens))

        # Smoothed IDF; +1 in the numerator keeps a term present in every rule at a small
        # positive weight rather than exactly zero.
        self._idf = {
            term: math.log((1 + n_docs) / (1 + df)) + 1.0 for term, df in doc_freq.items()
        }

        for rule_id, tokens in docs.items():
            self._vectors[rule_id] = self._vectorise(tokens)

    def _vectorise(self, tokens: list[str]) -> dict[str, float]:
        if not tokens:
            return {}
        counts = Counter(tokens)
        total = sum(counts.values())
        vector = {
            term: (count / total) * self._idf.get(term, 1.0) for term, count in counts.items()
        }
        norm = math.sqrt(sum(v * v for v in vector.values()))
        if norm == 0:
            return {}
        return {term: value / norm for term, value in vector.items()}

    # ---------- cache ----------

    def cache_signature(self) -> str:
        """Content hash of the pack. Changing a rule invalidates the cache automatically."""
        import hashlib

        digest = hashlib.sha256()
        for rule in sorted(self._rules, key=lambda r: r.rule_id):
            digest.update(rule.rule_id.encode())
            digest.update(rule.searchable_text.encode())
        return digest.hexdigest()[:16]

    def save_cache(self, cache_dir: str | Path | None = None) -> Path:
        directory = Path(cache_dir or CACHE_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / CACHE_FILE
        path.write_text(
            json.dumps(
                {
                    "signature": self.cache_signature(),
                    "idf": self._idf,
                    "vectors": self._vectors,
                },
                indent=0,
            ),
            encoding="utf-8",
        )
        return path

    def load_cache(self, cache_dir: str | Path | None = None) -> bool:
        """Returns True if a cache matching the current pack was loaded."""
        path = Path(cache_dir or CACHE_DIR) / CACHE_FILE
        if not path.exists():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False  # a corrupt cache must never break a review
        if payload.get("signature") != self.cache_signature():
            return False
        self._idf = payload["idf"]
        self._vectors = payload["vectors"]
        return True

    # ---------- retrieval ----------

    def _prefilter(self, changed: ChangedNode) -> tuple[list[Rule], list[tuple[str, str]]]:
        kept: list[Rule] = []
        eliminated: list[tuple[str, str]] = []
        path = (changed.file.path or "").replace("\\", "/")
        kind = changed.node.kind.value

        for rule in self._rules:
            if rule.node_kinds and kind not in rule.node_kinds:
                eliminated.append((rule.rule_id, f"node kind {kind} not in {list(rule.node_kinds)}"))
                continue
            if rule.path_globs and not any(fnmatch(path, g) for g in rule.path_globs):
                eliminated.append((rule.rule_id, f"path {path} matches no glob"))
                continue

            failed = None
            for key, expected in rule.requires:
                actual = {
                    "is_contracted": changed.node.is_contracted,
                    "materialization": changed.node.materialization,
                    "access": changed.node.access,
                }.get(key)
                if actual != expected:
                    failed = f"requires {key}={expected!r}, node has {actual!r}"
                    break
            if failed:
                eliminated.append((rule.rule_id, failed))
                continue

            kept.append(rule)

        return kept, eliminated

    def retrieve(
        self, changed: ChangedNode, change_summary: str, top_k: int = 3
    ) -> RetrievalResult:
        candidates, eliminated = self._prefilter(changed)
        query_tokens = _tokenize(change_summary)
        query_vector = self._vectorise(query_tokens)
        query_set = set(query_tokens)

        scored: list[RetrievedRule] = []
        for rule in candidates:
            vector = self._vectors.get(rule.rule_id, {})
            score = sum(weight * query_vector.get(term, 0.0) for term, weight in vector.items())
            matched = tuple(
                sorted(k for k in rule.keywords if set(_tokenize(k)) & query_set)
            )
            scored.append(RetrievedRule(rule=rule, score=score, matched_keywords=matched))

        # An exact keyword hit is curated evidence that the rule applies, so it outranks a
        # marginally higher cosine with no keyword overlap. Without this, long descriptive
        # rules crowd out short precise ones.
        scored.sort(key=lambda r: (bool(r.matched_keywords), r.score), reverse=True)

        # A rule needs real evidence to be cited, not merely a surviving prefilter.
        # `score > 0` alone is too weak: TF-IDF gives a small positive cosine to any
        # incidental shared token, which is how a policy engine ends up citing a PII rule
        # at a whitespace diff. Requiring either a curated keyword hit or a score above a
        # floor is what produced a usable correct-silence rate.
        retrieved = [
            r for r in scored if r.matched_keywords or r.score >= _MIN_SCORE
        ][:top_k]

        return RetrievalResult(
            retrieved=retrieved,
            eliminated=eliminated,
            query_terms=tuple(sorted(query_set)),
        )


def summarise_change(changed: ChangedNode) -> str:
    """Build the retrieval query from the change.

    Deliberately structural rather than a raw diff dump: the node's kind, its
    materialization, what happened to its columns, and the changed lines. Feeding the
    whole diff in lets boilerplate SQL dominate the vocabulary.
    """
    node = changed.node

    # A comment or whitespace edit changes nothing a governance rule can apply to, and
    # any vocabulary drawn from the node itself would be ambient rather than evidence.
    # Returning an empty query is what lets the caller stay silent.
    if not changed.has_semantic_change:
        return ""

    # Only what CHANGED goes in the query. The node's own attributes — its path, its
    # access level, its materialization — are true on every diff that touches it, so
    # feeding them in makes `stg_` match staging-layer-purity and `protected` match
    # public-access-review on every staging PR regardless of content. That drove the
    # correct-silence rate to 0.273 on the first measured run: ambient attributes are
    # not evidence of a violation.
    parts: list[str] = [changed.change_type.value]

    if changed.removed_columns:
        parts += ["removed column", "dropped column", *changed.removed_columns]
    if changed.added_columns:
        parts += ["added column", *changed.added_columns]
    if changed.change_type in (ChangeType.DELETED, ChangeType.RENAMED):
        parts += ["deleted model", "renamed", "rename"]
    if node.kind is NodeKind.EXPOSURE:
        parts += ["exposure", "owner"]

    # The changed lines carry the vocabulary that actually identifies a rule: `distinct`,
    # `left join`, `is_incremental`, `materialized`, `access`, `data_type`.
    parts.extend(changed.file.added_lines)
    parts.extend(changed.file.removed_lines)

    return " ".join(parts)
