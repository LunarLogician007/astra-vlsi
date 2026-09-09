#!/usr/bin/env python3
"""Dr. RTL scoring: the scalar objective, candidate selection, and the
group-relative advantage signal.

This is the paper's math, and nothing else -- no I/O, no tool invocation, so
it can be unit-tested without an EDA install.

    Eq. 1   min_D f(WNS, TNS, Area)   s.t.  D == D_0
    Eq. 3   Score_i = a*WNS^n + b*TNS^n + g*Area^n + penalty_i
    Eq. 4   D_{t+1} = argmin Score_i   s.t.  SEC_i = 1
    Eq. 5   A_i = (score_i - mu_t) / sigma_t

Lower Score is better throughout.

One deliberate generalisation of Eq. 3
--------------------------------------
The paper writes PPA^norm = (PPA_i - PPA_base) / PPA_base for every metric.
For area that is well behaved. For WNS/TNS it is only well behaved while the
baseline is *negative* -- which is the paper's regime, since it optimises
designs that are already failing timing. Once a design closes (WNS_base >= 0)
the denominator's sign flips and the formula starts rewarding regressions, and
at WNS_base == 0 it divides by zero.

So timing metrics are normalised as

    norm = -(x_i - x_base) / max(|x_base|, floor)

which is *algebraically identical* to the paper whenever x_base < 0 -- because
dividing by a negative is the same as negating and dividing by its magnitude
-- and keeps "more slack is better" true on the other side of zero. The floor
is the clock period, so a design sitting at exactly 0 ns still produces a
finite, sanely scaled number.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

# Paper footnote 2, p.4: "we set a = 0.5, b = 0.35, and g = 0.15. We set
# penalty_i = 0.5 if Area^norm_i > 0.1, and 0 otherwise, to discourage
# excessive area overhead."
DEFAULT_WEIGHTS: dict[str, float] = {
    "alpha": 0.50,          # WNS
    "beta": 0.35,           # TNS
    "gamma": 0.15,          # area
    "area_penalty": 0.50,   # flat adder, not a slope
    "area_threshold": 0.10,  # area_norm above this trips the penalty
}

# Denominator floor for timing metrics when the baseline is at (or very near)
# zero, expressed as a fraction of the clock period.
_ZERO_BAND = 1e-3


class ScoreError(ValueError):
    """Raised when a candidate cannot be scored at all."""


# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------


def norm_timing(value: float, baseline: float, period_ns: float = 1.0) -> float:
    """Normalised timing delta. Negative = the candidate improved.

    Identical to the paper's (x_i - x_base)/x_base for x_base < 0; see the
    module docstring for why the general form is written this way.
    """
    scale = max(abs(period_ns), _ZERO_BAND)
    # Anything inside a thousandth of a clock period counts as "zero baseline"
    # and is normalised against the period instead of against itself.
    denom = abs(baseline) if abs(baseline) > scale * _ZERO_BAND else scale
    return -(value - baseline) / denom


def norm_area(value: float, baseline: float) -> float:
    """Normalised area delta. Positive = the candidate got bigger."""
    if baseline is None or abs(baseline) < 1e-12:
        return 0.0
    return (value - baseline) / abs(baseline)


def normalize(metrics: dict[str, Any], baseline: dict[str, Any],
              period_ns: float = 1.0) -> dict[str, float]:
    """All three normalised terms for one candidate.

    A missing metric normalises to 0.0 -- "no evidence of change" -- rather
    than silently scoring the candidate as an improvement.
    """
    def pick(d: dict[str, Any], *keys: str) -> float | None:
        for k in keys:
            v = d.get(k)
            if v is not None:
                return float(v)
        return None

    out: dict[str, float] = {}
    for key, names, fn in (
        ("wns", ("wns_ns", "wns"), norm_timing),
        ("tns", ("tns_ns", "tns"), norm_timing),
    ):
        v, b = pick(metrics, *names), pick(baseline, *names)
        out[key] = 0.0 if v is None or b is None else fn(v, b, period_ns)

    v, b = pick(metrics, "area_um2", "area"), pick(baseline, "area_um2", "area")
    out["area"] = 0.0 if v is None or b is None else norm_area(v, b)
    return out


# ---------------------------------------------------------------------------
# Eq. 3 -- the scalar score
# ---------------------------------------------------------------------------


def score(metrics: dict[str, Any], baseline: dict[str, Any],
          weights: dict[str, float] | None = None,
          period_ns: float = 1.0) -> dict[str, Any]:
    """Eq. 3. Returns the score plus every term that went into it.

    The breakdown is kept because the skill-learning agent reasons about *why*
    a candidate won, not just that it did.
    """
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    n = normalize(metrics, baseline, period_ns)

    penalty = w["area_penalty"] if n["area"] > w["area_threshold"] else 0.0
    total = (w["alpha"] * n["wns"]
             + w["beta"] * n["tns"]
             + w["gamma"] * n["area"]
             + penalty)

    return {
        "score": total,
        "terms": {
            "wns": w["alpha"] * n["wns"],
            "tns": w["beta"] * n["tns"],
            "area": w["gamma"] * n["area"],
            "penalty": penalty,
        },
        "normalized": n,
        "weights": w,
    }


def score_value(metrics: dict[str, Any], baseline: dict[str, Any],
                weights: dict[str, float] | None = None,
                period_ns: float = 1.0) -> float:
    return float(score(metrics, baseline, weights, period_ns)["score"])


# ---------------------------------------------------------------------------
# Eq. 4 -- selection under the SEC constraint
# ---------------------------------------------------------------------------


def sec_passed(cand: dict[str, Any]) -> bool:
    """SEC_i in {0, 1}. Anything that is not an explicit pass is a fail.

    An unrun or crashed equivalence check is not evidence of equivalence, so
    it must not be allowed to promote a candidate.
    """
    sec = cand.get("sec")
    if isinstance(sec, dict):
        return sec.get("equivalent") is True
    return sec is True or sec == 1


def sec_decided(cand: dict[str, Any]) -> bool:
    """Did the equivalence check actually reach a verdict?

    A timeout or a crashed tool did not. That distinction does not matter for
    Eq. 4 -- an undecided candidate still cannot be promoted -- but it matters
    enormously for skill learning: recording "this transformation breaks
    equivalence" because the SAT solver ran out of time would condemn a
    perfectly good strategy on evidence that does not exist.
    """
    sec = cand.get("sec")
    if not isinstance(sec, dict):
        return sec is not None
    if sec.get("equivalent") is True:
        return True
    return sec.get("method") not in ("error", "skipped", None)


def select_best(candidates: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """Eq. 4: argmin Score_i subject to SEC_i = 1.

    Candidates must already carry a "score". Ties break on the earlier
    candidate, which keeps a rerun of the same group deterministic.
    """
    eligible = [c for c in candidates
                if sec_passed(c) and c.get("score") is not None]
    if not eligible:
        return None
    return min(eligible, key=lambda c: (c["score"], c.get("id", "")))


# ---------------------------------------------------------------------------
# Eq. 5 -- group-relative advantage
# ---------------------------------------------------------------------------


def advantages(candidates: list[dict[str, Any]],
               include_failed: bool = False) -> list[dict[str, Any]]:
    """Eq. 5: A_i = (score_i - mu_t) / sigma_t over one iteration's group.

    mu and sigma are the *population* moments of the group (the group is the
    whole population being compared, not a sample of a larger one).

    SEC-failing candidates are excluded from mu/sigma by default: a rewrite
    that changed the function is not a point in the design space being
    compared, and letting its score drag the mean would distort every sibling
    advantage. They are still annotated, with A_i = None.

    Degenerate groups -- one candidate, or all scores identical -- have no
    spread to normalise against, so every advantage is 0.0 rather than an
    infinity. Nothing is learned from a group that says nothing.
    """
    pool = [c for c in candidates
            if c.get("score") is not None and (include_failed or sec_passed(c))]
    scores = [float(c["score"]) for c in pool]

    if len(scores) >= 2:
        mu = sum(scores) / len(scores)
        var = sum((s - mu) ** 2 for s in scores) / len(scores)
        sigma = math.sqrt(var)
    elif scores:
        mu, sigma = scores[0], 0.0
    else:
        mu, sigma = 0.0, 0.0

    ids = {id(c) for c in pool}
    for c in candidates:
        if id(c) not in ids:
            c["advantage"] = None
            continue
        c["advantage"] = 0.0 if sigma < 1e-12 else (float(c["score"]) - mu) / sigma
    return candidates


def sec_tally(candidates: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """SEC outcomes, keeping "not generated" apart from "not equivalent".

    A candidate the model never produced -- a CLI failure, a reply with no
    Verilog in it -- has said nothing about whether the transformation is
    sound, and a timed-out solver has said nothing either. Folding those in
    with refutations understates the pass rate by however unreliable the
    harness happened to be that day, which is a fact about the harness
    reported as a fact about the method. On one real run it turned three of
    three candidates passing into a reported 33%, because six of nine model
    calls had died before writing anything.

    The distinction already exists in `sec_decided`; this is the reporting
    that uses it.
    """
    cands = list(candidates)
    generated = [c for c in cands if not c.get("error")]
    decided = [c for c in generated if sec_decided(c)]
    passed = [c for c in decided if sec_passed(c)]
    return {
        "total": len(cands),
        "not_generated": len(cands) - len(generated),
        "undecided": len(generated) - len(decided),
        "decided": len(decided),
        "passed": len(passed),
        "rate": (len(passed) / len(decided)) if decided else None,
    }


def render_sec_tally(t: dict[str, Any]) -> str:
    """One line, with the caveats inline rather than in a footnote."""
    if not t["decided"]:
        return f"SEC: no candidate reached a verdict ({t['total']} attempted)"
    out = f"SEC pass rate {t['passed']}/{t['decided']} ({t['rate']:.0%} of decided)"
    extra = []
    if t["not_generated"]:
        extra.append(f"{t['not_generated']} never generated")
    if t["undecided"]:
        extra.append(f"{t['undecided']} undecided (solver timeout)")
    if extra:
        out += " -- " + ", ".join(extra) + ", excluded"
    return out


def group_stats(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """mu_t, sigma_t and group size, for the trajectory log."""
    scores = [float(c["score"]) for c in candidates
              if c.get("score") is not None and sec_passed(c)]
    if not scores:
        return {"n": 0, "mean": None, "std": None}
    mu = sum(scores) / len(scores)
    var = sum((s - mu) ** 2 for s in scores) / len(scores)
    return {"n": len(scores), "mean": mu, "std": math.sqrt(var)}


# ---------------------------------------------------------------------------
# convenience: score a whole group in place
# ---------------------------------------------------------------------------


def score_group(candidates: list[dict[str, Any]], baseline: dict[str, Any],
                weights: dict[str, float] | None = None,
                period_ns: float = 1.0) -> list[dict[str, Any]]:
    """Attach score, breakdown and advantage to every candidate in a group.

    A candidate whose synthesis or STA failed has no metrics to score; it is
    left with score None so that Eq. 4 skips it and Eq. 5 excludes it.
    """
    for c in candidates:
        m = c.get("metrics") or {}
        if m.get("wns_ns") is None and m.get("area_um2") is None:
            c["score"] = None
            c["score_detail"] = None
            continue
        detail = score(m, baseline, weights, period_ns)
        c["score"] = detail["score"]
        c["score_detail"] = detail
    return advantages(candidates)
