#!/usr/bin/env python3
"""The confidence-aware skill library -- paper Sec. 4.2, "Skill update".

A skill is one pattern-strategy pair: a recurring structural bottleneck on a
critical path, and the transformation principle that fixes it. The library
persists across runs and across designs, which is the whole point -- it is
what turns a per-design optimiser into a self-improving one.

Each entry accumulates the three statistics the paper names -- occurrence
count, SEC-pass count, and mean relative advantage -- and those three produce
a confidence that decides whether the entry is offered to the optimisation
agent, ignored, or actively flagged as an invalid strategy.

On seeded entries
-----------------
The paper's library holds 47 entries *learned* from its own runs. A library
shipped with pre-baked statistics would be fabricated evidence, so the seed
set here carries `source: "seed"`, zero occurrences and zero confidence: the
transformations are offered as untested suggestions and have to earn their
statistics from real runs like anything else.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(os.environ.get("ASTRA_ROOT", Path(__file__).resolve().parent.parent))
LIBRARY = Path(os.environ.get("ASTRA_SKILLS", ROOT / "skills" / "library.json"))

# Statistical shrinkage: an entry seen once should not outrank one seen ten
# times just because its single trial went well.
_SUPPORT_K = 3.0
# Advantage is a z-score; half a standard deviation of separation is treated
# as a full unit of evidence when squashing it into [0, 1].
_ADV_SCALE = 0.5

EFFECTIVE_AT = 0.55      # confidence at or above which an entry is promoted
INVALID_SEC_RATE = 0.5   # SEC pass rate below which an entry is condemned
MIN_TRIALS_TO_CONDEMN = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# text matching
# ---------------------------------------------------------------------------

_STOP = {"the", "a", "an", "of", "on", "in", "to", "and", "or", "with", "for",
         "is", "at", "by", "into", "from", "that", "this", "its"}


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower())
            if t not in _STOP and len(t) > 2}


def similarity(a: str, b: str) -> float:
    """Jaccard overlap of content words. Cheap, deterministic, no deps.

    Good enough because the vocabulary here is small and technical: "wide
    arithmetic carry propagation" and "carry chain in wide arithmetic" share
    the words that matter and nothing else does.
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def make_id(pattern: str, strategy: str) -> str:
    def slug(s: str) -> str:
        return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (s or "").lower())).strip("-")
    return f"{slug(pattern)[:48]}::{slug(strategy)[:48]}"


# ---------------------------------------------------------------------------
# confidence
# ---------------------------------------------------------------------------


def confidence(stats: dict[str, Any]) -> float:
    """Fold the three recorded statistics into one number in [0, 1].

        correctness  SEC pass rate -- a rewrite that breaks the design is
                     worth nothing regardless of how fast it was.
        benefit      logistic squash of the mean relative advantage. Advantage
                     is negated first because Eq. 3 scores are minimised, so a
                     *negative* A_i is the candidate that beat its peers.
        support      n/(n+k) shrinkage, so one lucky trial stays provisional.

    The product, not the sum: each factor is a veto. Zero SEC passes, no
    measured benefit, or no trials at all should each drive confidence to the
    floor on its own.
    """
    n = float(stats.get("occurrences") or 0)
    if n <= 0:
        return 0.0
    sec_rate = float(stats.get("sec_pass") or 0) / n

    adv_n = float(stats.get("advantage_n") or 0)
    mean_adv = (float(stats.get("advantage_sum") or 0.0) / adv_n) if adv_n else 0.0
    benefit = 1.0 / (1.0 + math.exp(mean_adv / _ADV_SCALE))

    support = n / (n + _SUPPORT_K)
    return round(sec_rate * benefit * support, 4)


def classify(stats: dict[str, Any], conf: float) -> str:
    n = float(stats.get("occurrences") or 0)
    sec_rate = (float(stats.get("sec_pass") or 0) / n) if n else 0.0
    if n >= MIN_TRIALS_TO_CONDEMN and sec_rate < INVALID_SEC_RATE:
        return "invalid"
    if conf >= EFFECTIVE_AT:
        return "effective"
    return "candidate"


def mean_advantage(stats: dict[str, Any]) -> float | None:
    n = float(stats.get("advantage_n") or 0)
    return (float(stats.get("advantage_sum") or 0.0) / n) if n else None


# ---------------------------------------------------------------------------
# library
# ---------------------------------------------------------------------------


class SkillLibrary:
    """A JSON-backed pattern-strategy store with empirical statistics."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or LIBRARY)
        self.entries: dict[str, dict[str, Any]] = {}
        self.meta: dict[str, Any] = {"version": 1, "created_at": _now()}
        self.load()

    # -- persistence --------------------------------------------------------

    def load(self) -> "SkillLibrary":
        if not self.path.is_file():
            return self
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return self
        self.meta = data.get("meta", self.meta)
        for e in data.get("skills", []):
            e.setdefault("id", make_id(e.get("pattern", ""), e.get("strategy", "")))
            self.entries[e["id"]] = e
        return self

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.meta["updated_at"] = _now()
        self.meta["count"] = len(self.entries)
        ordered = sorted(self.entries.values(),
                         key=lambda e: (-float(e.get("confidence") or 0.0), e["id"]))
        self.path.write_text(json.dumps(
            {"meta": self.meta, "skills": ordered}, indent=2) + "\n")
        return self.path

    # -- reading ------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.entries)

    def all(self, include_invalid: bool = False) -> list[dict[str, Any]]:
        out = [e for e in self.entries.values()
               if include_invalid or e.get("status") != "invalid"]
        return sorted(out, key=lambda e: (-float(e.get("confidence") or 0.0), e["id"]))

    def invalid(self) -> list[dict[str, Any]]:
        return [e for e in self.entries.values() if e.get("status") == "invalid"]

    def match(self, patterns: Iterable[str], limit: int = 6,
              threshold: float = 0.2) -> list[dict[str, Any]]:
        """Entries whose pattern resembles any diagnosed bottleneck.

        Invalid entries are deliberately *kept* in the result, marked as such:
        telling the optimisation agent "this was tried three times and broke
        the design twice" is more useful than silently letting it rediscover
        the same dead end.
        """
        wanted = [p for p in patterns if p]
        scored: list[tuple[float, dict[str, Any]]] = []
        for e in self.entries.values():
            best = max((similarity(p, e.get("pattern", "")) for p in wanted),
                       default=0.0)
            if best >= threshold:
                scored.append((best, e))
        scored.sort(key=lambda t: (-t[0], -float(t[1].get("confidence") or 0.0)))
        return [dict(e, match_score=round(s, 3)) for s, e in scored[:limit]]

    # -- writing ------------------------------------------------------------

    def record(self, pattern: str, strategy: str, *, sec_pass: bool,
               advantage: float | None, design: str, iteration: int,
               rationale: str = "", example: str = "",
               score_delta: float | None = None,
               conclusive: bool = True) -> dict[str, Any]:
        """Merge one observed trajectory outcome into the library.

        Merging is by *similarity*, not by exact string: the optimisation
        agent phrases the same bottleneck differently from run to run, and
        forking a near-duplicate entry each time would keep every entry's
        statistics too thin to ever clear the support term.
        """
        entry = self._find_similar(pattern, strategy)
        if entry is None:
            entry = {
                "id": make_id(pattern, strategy),
                "pattern": pattern,
                "strategy": strategy,
                "rationale": rationale,
                "example": example,
                "source": "learned",
                "created_at": _now(),
                "stats": {"occurrences": 0, "sec_pass": 0, "sec_fail": 0,
                          "inconclusive": 0,
                          "advantage_sum": 0.0, "advantage_n": 0,
                          "score_delta_sum": 0.0, "score_delta_n": 0,
                          "designs": [], "iterations": 0},
            }
            self.entries[entry["id"]] = entry

        st = entry["stats"]
        st.setdefault("inconclusive", 0)
        st["iterations"] += 1
        if conclusive:
            st["occurrences"] += 1
            st["sec_pass" if sec_pass else "sec_fail"] += 1
        else:
            # The check timed out or crashed. That is a fact about the solver,
            # not about the transformation, so it feeds neither the SEC pass
            # rate nor the support term -- it is only counted so the record
            # shows why an entry has fewer trials than iterations.
            st["inconclusive"] += 1
        if design and design not in st["designs"]:
            st["designs"].append(design)
        # Only SEC-passing candidates contribute an advantage: a broken
        # rewrite has no place in the group whose mean and sigma produced it.
        if advantage is not None and sec_pass and conclusive:
            st["advantage_sum"] += float(advantage)
            st["advantage_n"] += 1
        if score_delta is not None and sec_pass and conclusive:
            st["score_delta_sum"] += float(score_delta)
            st["score_delta_n"] += 1

        if rationale and not entry.get("rationale"):
            entry["rationale"] = rationale
        if example and not entry.get("example"):
            entry["example"] = example
        if entry.get("source") == "seed":
            entry["source"] = "seed+learned"

        entry["last_seen"] = {"design": design, "iteration": iteration, "at": _now()}
        entry["confidence"] = confidence(st)
        entry["mean_advantage"] = mean_advantage(st)
        entry["status"] = classify(st, entry["confidence"])
        entry["updated_at"] = _now()
        return entry

    def _find_similar(self, pattern: str, strategy: str,
                      threshold: float = 0.6) -> dict[str, Any] | None:
        exact = self.entries.get(make_id(pattern, strategy))
        if exact:
            return exact
        best, best_score = None, threshold
        for e in self.entries.values():
            s = 0.5 * similarity(pattern, e.get("pattern", "")) \
                + 0.5 * similarity(strategy, e.get("strategy", ""))
            if s > best_score:
                best, best_score = e, s
        return best

    def add_seed(self, pattern: str, strategy: str, rationale: str = "",
                 example: str = "") -> dict[str, Any]:
        """A suggestion with no evidence behind it yet."""
        sid = make_id(pattern, strategy)
        if sid in self.entries:
            return self.entries[sid]
        entry = {
            "id": sid, "pattern": pattern, "strategy": strategy,
            "rationale": rationale, "example": example,
            "source": "seed", "created_at": _now(),
            "stats": {"occurrences": 0, "sec_pass": 0, "sec_fail": 0,
                      "inconclusive": 0,
                      "advantage_sum": 0.0, "advantage_n": 0,
                      "score_delta_sum": 0.0, "score_delta_n": 0,
                      "designs": [], "iterations": 0},
            "confidence": 0.0, "mean_advantage": None, "status": "candidate",
        }
        self.entries[sid] = entry
        return entry


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def render(entries: list[dict[str, Any]], with_examples: bool = True) -> str:
    """Format entries for the optimisation agent's prompt."""
    if not entries:
        return "(the skill library is empty -- nothing learned yet)"
    lines: list[str] = []
    for e in entries:
        st = e.get("stats") or {}
        n = st.get("occurrences", 0)
        adv = e.get("mean_advantage")
        tag = {"effective": "PROVEN", "invalid": "KNOWN BAD",
               "candidate": "UNTESTED" if not n else "PROVISIONAL"}.get(
                   e.get("status", "candidate"), "?")
        lines.append(f"- [{tag}] {e['pattern']}  ->  {e['strategy']}")
        if e.get("rationale"):
            lines.append(f"    why: {e['rationale']}")
        stat = (f"    record: {n} decided use(s), SEC pass "
                f"{st.get('sec_pass', 0)}/{n or 0}, "
                f"confidence {e.get('confidence', 0.0):.2f}")
        if st.get("inconclusive"):
            stat += f", {st['inconclusive']} undecided (solver timeout)"
        if adv is not None:
            stat += f", mean advantage {adv:+.3f}"
        if st.get("designs"):
            stat += f", seen on {', '.join(st['designs'][:4])}"
        lines.append(stat)
        if e.get("status") == "invalid":
            lines.append("    NOTE: this has broken equivalence more often than "
                         "not. Do not apply it unless you can say why this case "
                         "differs.")
        if with_examples and e.get("example"):
            lines.append("    example:")
            for ln in str(e["example"]).splitlines()[:10]:
                lines.append(f"      {ln}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="astra-skills",
                                 description="Inspect the skill library.")
    ap.add_argument("action", nargs="?", default="list",
                    choices=("list", "json", "match", "stats"))
    ap.add_argument("query", nargs="*", help="pattern text, for `match`")
    ap.add_argument("--path", type=Path, default=None)
    ap.add_argument("--all", action="store_true", help="include invalid entries")
    args = ap.parse_args(argv[1:])

    lib = SkillLibrary(args.path)
    if args.action == "json":
        print(json.dumps({"meta": lib.meta, "skills": lib.all(True)}, indent=2))
    elif args.action == "match":
        print(render(lib.match([" ".join(args.query)])))
    elif args.action == "stats":
        entries = lib.all(True)
        by = {}
        for e in entries:
            by[e.get("status", "?")] = by.get(e.get("status", "?"), 0) + 1
        print(f"library: {lib.path}")
        print(f"entries: {len(entries)}")
        for k, v in sorted(by.items()):
            print(f"  {k:<10} {v}")
    else:
        print(render(lib.all(args.all), with_examples=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
