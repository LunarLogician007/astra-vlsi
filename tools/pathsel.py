#!/usr/bin/env python3
"""Path-portfolio selection: which bottlenecks are worth an agent call.

Ranking paths by slack answers "which endpoint is worst". The optimiser needs a
different question answered -- "which *distinct* pieces of this design are worth
attacking in parallel" -- and on real reports the two answers are nothing alike.

Measured on the reference design's own committed run: OpenSTA reports 20 paths,
they share a single startpoint, they end on 20 adjacent bits of one register
bank, and their RTL region sets have a pairwise Jaccard of 1.00. Handing the
top three to three agents gets three rewrites of the same accumulate chain.

So selection happens in two stages, and both are mechanical:

1.  Cluster the reported paths into distinct logic *cones*, by how much RTL,
    which cone-origin nets and which cell mix they share.
2.  If fewer cones exist than agents to spend -- the common case on a small
    design, and the measured case here -- cut the best cone's representative
    path into contiguous *segments* whose cell-family signature differs. A
    91-stage path through an AND partial-product array, then a carry region,
    then a saturation reduction is three separately attackable targets even
    though it is one path.

Both kinds come out as a PathTarget with the same shape, so everything
downstream is indifferent to which one it got.

k is a ceiling, not a quota. A design with one small cone and no valid split
yields one target and one agent. Manufacturing three targets where the design
has one would be fabricating diversity, and the trajectory would record it as
though the tool had found something.

Nothing here calls a model or a tool. It is arithmetic over the parsed report,
so it is unit-testable against synthetic path dicts.
"""

from __future__ import annotations

import json
import math
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import clocks  # noqa: E402
import rtl_map  # noqa: E402

# --------------------------------------------------------------------------
# tuning
# --------------------------------------------------------------------------

# Similarity weights. Cone origins carry the most because they are the only
# term measured to discriminate on a real report: region sets came back at
# Jaccard 1.00 across every pair of the reference design's 20 paths, while
# cone origins -- which keep the bit index, so acc_out[9] and acc_out[36] stay
# distinct points in one cone -- spread across 0.79 to 0.96.
SIM_WEIGHTS: dict[str, float] = {
    "regions": 0.30,
    "origins": 0.40,
    "families": 0.20,
    "endpoints": 0.10,
}

# Above this, two paths are the same bottleneck. Calibrated against that same
# report, where cross-path similarity works out near 0.93 and the whole pool
# correctly collapses to a single cluster.
CLUSTER_AT = 0.65

# Value weights. Mass leads: the cluster that owns most of the failure is the
# one worth fixing, whatever its worst path's slack happens to be.
VALUE_WEIGHTS: dict[str, float] = {"mass": 0.45,          # impact
                                   "criticality": 0.35,   # severity
                                   "tractability": 0.20}

# Delay uncertainty used to turn a deterministic report into a criticality
# distribution, as a fraction of the clock period. Post-synthesis STA has no
# real wire delay in it, so the reported ordering of two near-tied paths is
# not yet settled -- this is how much they are assumed able to move before
# layout. Set it to 0 to recover the deterministic answer exactly.
DELAY_SIGMA = 0.05
# Share of that uncertainty common to a whole cone. Paths through one cone
# traverse mostly the same cells, so their errors move together; paths in
# different cones are near-independent.
CONE_RHO = 0.7
MC_SAMPLES = 20000
MC_SEED = 20260908       # fixed: the whole pipeline is reproducible

MMR_LAMBDA = 0.5      # diversity weight in the marginal-relevance selection
MASS_FLOOR = 0.02     # a cluster owning under 2% of the failure is not a target
# A segment worth its own agent must own a real share of the path. The floor
# scales with k so that asking for more segments cannot produce slivers:
# min share = SEG_SHARE_OF_EVEN / k, i.e. half of an even split.
SEG_SHARE_OF_EVEN = 0.5
SEG_MIN_CELLS = 4     # combinational cells; flops at a boundary do not count
SCOPE_SLACK = 3       # lines of slop around a target's regions

_STAGE_DIRS = {"sta": "02_sta", "pnr": "03_pnr"}


# --------------------------------------------------------------------------
# set and histogram metrics
# --------------------------------------------------------------------------


def jaccard(a: set[str] | frozenset[str], b: set[str] | frozenset[str]) -> float:
    """|A n B| / |A u B|. Two empty sets are not similar, they are unknown."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def weighted_jaccard(a: dict[Any, float], b: dict[Any, float]) -> float:
    """Ruzicka similarity: sum(min) / sum(max) over the shared key space.

    Plain Jaccard on region *sets* calls two paths identical when both touch
    the same line, even if one spends 90% of its delay there and the other 2%.
    The weights are delay shares, so this keeps that distinction.
    """
    if not a or not b:
        return 0.0
    keys = set(a) | set(b)
    lo = sum(min(a.get(k, 0.0), b.get(k, 0.0)) for k in keys)
    hi = sum(max(a.get(k, 0.0), b.get(k, 0.0)) for k in keys)
    return lo / hi if hi > 0 else 0.0


def hist_similarity(a: dict[str, float], b: dict[str, float]) -> float:
    """1 - total variation distance between two normalised histograms."""
    if not a or not b:
        return 0.0
    keys = set(a) | set(b)
    tv = 0.5 * sum(abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in keys)
    return max(0.0, 1.0 - tv)


def _normalise(counts: dict[str, float] | Counter) -> dict[str, float]:
    total = float(sum(counts.values()))
    if total <= 0:
        return {}
    return {k: v / total for k, v in counts.items()}


# --------------------------------------------------------------------------
# per-path features
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PathFeatures:
    index: int
    slack_ns: float | None
    status: str | None
    startpoint: str
    endpoint: str
    start_base: str
    end_base: str
    regions: dict[tuple[str, int, int], float]
    origins: frozenset[str]
    families: dict[str, float]
    coverage: float
    depth: int
    # The period of the clock that captured THIS path, not the design's. On a
    # multi-clock design these differ, and normalising every path against one
    # of them is the silent mis-ranking HANDOFF.md section 6.1 describes.
    path_group: str | None = None
    period_ns: float | None = None
    period_exact: bool = True
    mapping: dict[str, Any] = field(default_factory=dict, repr=False)
    diagnosis: dict[str, Any] = field(default_factory=dict, repr=False)


class _OnePeriod:
    """Back-compat shim: a bare float means one period for every group."""

    is_multi = False

    def __init__(self, period_ns: float | None) -> None:
        self._p = period_ns

    def resolve_period(self, group: str | None) -> tuple[float | None, bool]:
        return self._p, True

    @property
    def primary_period(self) -> float | None:
        return self._p


def period_book(period_ns: Any) -> Any:
    """Normalise the period argument into something that resolves a group.

    Accepts a ``clocks.ClockSet``, a bare float (the single-clock case, which
    every caller predating multi-clock support passes), or None.
    """
    if period_ns is None or isinstance(period_ns, (int, float)):
        return _OnePeriod(float(period_ns) if period_ns else None)
    return period_ns


def _origins(path: dict[str, Any]) -> frozenset[str]:
    """Cone-origin nets the path's stages resolve to, bit index kept.

    Deliberately not run through ``base_ident``: yosys autoname names every
    abc-generated cell after the net its cone terminates at, so dropping the
    bit select would map every stage of every path in a register bank onto one
    identifier and the feature would carry no information at all. The bit index
    is the only thing left that separates two paths through the same cone.
    """
    out: set[str] = set()
    for st in path.get("stages") or []:
        inst, _ = rtl_map.split_pin(st.get("pin") or "")
        name = rtl_map.strip_autoname(inst)
        if name and not rtl_map.is_mangled(name):
            out.add(name)
    return frozenset(out)


def _region_weights(mapping: dict[str, Any]) -> dict[tuple[str, int, int], float]:
    """Region -> share of the path's localised delay."""
    regions = mapping.get("regions") or []
    weights = {rtl_map._region_key(r): float(r.get("delay_ns") or 0.0)
               for r in regions}
    total = sum(weights.values())
    if total <= 0:      # every region measured zero delay: weight them evenly
        return {k: 1.0 / len(weights) for k in weights} if weights else {}
    return {k: v / total for k, v in weights.items()}


def path_features(path: dict[str, Any], index: int, nl: dict[str, Any],
                  rtl: rtl_map.RtlIndex, period_ns: Any) -> PathFeatures:
    book = period_book(period_ns)
    group = path.get("path_group")
    own_period, exact = book.resolve_period(group)
    mapping = rtl_map.map_path(path, nl, rtl)
    diagnosis = rtl_map.diagnose(path, own_period)
    return PathFeatures(
        index=index,
        slack_ns=path.get("slack_ns"),
        status=path.get("status"),
        path_group=group,
        period_ns=own_period,
        period_exact=exact,
        startpoint=path.get("startpoint") or "",
        endpoint=path.get("endpoint") or "",
        start_base=rtl_map.base_ident(rtl_map.strip_autoname(path.get("startpoint") or "")),
        end_base=rtl_map.base_ident(rtl_map.strip_autoname(path.get("endpoint") or "")),
        regions=_region_weights(mapping),
        origins=_origins(path),
        families=_normalise(Counter(diagnosis.get("cell_families") or {})),
        coverage=float((mapping.get("coverage") or {}).get("fraction") or 0.0),
        depth=int(diagnosis.get("logic_depth") or 0),
        mapping=mapping,
        diagnosis=diagnosis,
    )


def similarity(a: PathFeatures, b: PathFeatures) -> float:
    """How much two paths are the same bottleneck, in [0, 1].

    A term neither path has evidence for is dropped and the remaining weights
    are renormalised, rather than scored as zero. Scoring absent evidence as
    dissimilarity would make a path only 0.70 similar to itself on any design
    where localisation fails, and would silently move every similarity away
    from the clustering threshold -- so the threshold would mean one thing on a
    design that localises and another on a design that does not.
    """
    w = SIM_WEIGHTS
    terms: list[tuple[float, float]] = []
    if a.regions or b.regions:
        terms.append((w["regions"], weighted_jaccard(a.regions, b.regions)))
    if a.origins or b.origins:
        terms.append((w["origins"], jaccard(a.origins, b.origins)))
    if a.families or b.families:
        terms.append((w["families"], hist_similarity(a.families, b.families)))
    terms.append((w["endpoints"],
                  0.5 * float(a.end_base == b.end_base)
                  + 0.5 * float(a.start_base == b.start_base)))
    total = sum(weight for weight, _ in terms)
    if total <= 0:
        return 0.0
    return sum(weight * value for weight, value in terms) / total


# --------------------------------------------------------------------------
# clustering
# --------------------------------------------------------------------------


def cluster(feats: list[PathFeatures],
            cluster_at: float = CLUSTER_AT) -> tuple[list[list[int]], list[list[float]]]:
    """Agglomerative average-linkage clustering. Returns (clusters, sim matrix).

    Average rather than single linkage: a chain of pairwise-similar paths is
    exactly what a register bank produces, and single linkage would happily
    merge two genuinely different cones through it. O(n^3) is irrelevant at the
    twenty-to-sixty paths a report carries.

    Deterministic throughout -- ties break on the lower path index, which is
    the worse slack -- so re-running on the same JSON gives the same portfolio.
    """
    n = len(feats)
    sim = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            s = similarity(feats[i], feats[j])
            sim[i][j] = sim[j][i] = s

    clusters: list[list[int]] = [[i] for i in range(n)]
    while len(clusters) > 1:
        best, best_pair = -1.0, None
        for x in range(len(clusters)):
            for y in range(x + 1, len(clusters)):
                link = sum(sim[i][j] for i in clusters[x] for j in clusters[y]) \
                    / (len(clusters[x]) * len(clusters[y]))
                if link > best:
                    best, best_pair = link, (x, y)
        if best_pair is None or best < cluster_at:
            break
        x, y = best_pair
        clusters[x] = sorted(clusters[x] + clusters[y])
        clusters.pop(y)
    clusters.sort(key=lambda c: min(c))
    return clusters, sim


def cluster_similarity(ca: list[int], cb: list[int],
                       sim: list[list[float]]) -> float:
    if not ca or not cb:
        return 0.0
    return sum(sim[i][j] for i in ca for j in cb) / (len(ca) * len(cb))


# --------------------------------------------------------------------------
# criticality distribution
# --------------------------------------------------------------------------


def criticality(feats: list[PathFeatures], clusters: list[list[int]],
                period_ns: float | None, sigma: float = DELAY_SIGMA,
                rho: float = CONE_RHO, samples: int = MC_SAMPLES,
                seed: int = MC_SEED) -> tuple[list[float], list[float]]:
    """P(each path -- and each cluster -- is the one limiting the clock).

    Static timing analysis is deterministic: a slack is computed, not
    estimated, and the worst path is known exactly. So this is NOT a
    probability that the design fails, and it is not statistical STA over
    process variation.

    It answers a different and, before layout, better-posed question: the
    post-synthesis report contains no real wire delay, so two paths a few
    picoseconds apart are not yet reliably ordered, and committing three
    agents to the nominally-worst one is a bet on an ordering the flow has
    not established. Each path delay is therefore treated as
    ``-slack_i + noise`` and the noise is sampled:

        noise_i = sigma * (sqrt(rho) * z_cone(i) + sqrt(1 - rho) * z_i)

    with ``sigma`` a fraction of the clock period. The cone term is shared,
    because paths through one cone traverse mostly the same cells and their
    errors move together -- without it, twenty bit-slices of one bottleneck
    would each be assigned 1/20 of the criticality and the cone that owns all
    of it would look unimportant.

    Slack is used rather than arrival so that paths in different clock groups
    are compared on the same scale, and so that this works unchanged on a
    design that MEETS timing: "which path limits the clock" is a live question
    whether or not it is currently being violated, which is the case for any
    design being pushed to a tighter period.

    sigma = 0 collapses to the deterministic answer -- all the weight on the
    worst path's cluster, split evenly across exact ties.
    """
    import random

    n = len(feats)
    if n == 0:
        return [], []
    of_cluster = {i: c for c, members in enumerate(clusters) for i in members}

    # Everything below is in CYCLES of each path's own clock, not nanoseconds.
    #
    # Raw slack cannot be compared across asynchronous domains. "Which path
    # limits the clock" presupposes one clock; with five masters there are
    # five, and a path 0.5 ns short of a 2 ns cycle is in far more trouble than
    # one 0.5 ns short of a 32 ns cycle even though the second has the worse
    # number. Dividing by each path's own period makes the question well posed
    # again -- it becomes "which path consumes most of its own budget".
    #
    # For a single-clock design this is a uniform division of every base and
    # every scale by the same period. argmax is invariant under that, so the
    # answer is arithmetically identical to the nanosecond form it replaces --
    # see TestCriticalityUnitsAreCycles.
    book = period_book(period_ns)
    fallback = book.primary_period if hasattr(book, "primary_period") else period_ns
    periods = [abs(f.period_ns or fallback or 1.0) for f in feats]

    # Worse slack = closer to limiting. A path with no slack reported cannot
    # be ranked, so it is parked far below anything that can.
    frac = [(-f.slack_ns / p) if f.slack_ns is not None else None
            for f, p in zip(feats, periods)]
    known = [x for x in frac if x is not None]
    floor = (min(known) - 1.0) if known else 0.0
    base = [x if x is not None else floor for x in frac]

    # sigma is already a fraction of a period, so in cycle units it IS the
    # scale -- the per-domain difference is now carried by base rather than
    # applied here.
    scale = abs(sigma)
    if scale <= 0 or n == 1:
        best = max(base)
        tied = [i for i, b in enumerate(base) if b >= best - 1e-12]
        per_path = [1.0 / len(tied) if i in tied else 0.0 for i in range(n)]
        return per_path, _by_cluster(per_path, clusters)

    rho = min(max(rho, 0.0), 1.0)
    root_rho, root_ind = math.sqrt(rho), math.sqrt(1.0 - rho)
    s_cone = [scale * root_rho] * n
    s_ind = [scale * root_ind] * n
    rng = random.Random(seed)
    wins = [0] * n

    for _ in range(samples):
        cone_z = [rng.gauss(0.0, 1.0) for _ in clusters]
        best_i, best_v = 0, float("-inf")
        for i in range(n):
            v = (base[i] + s_cone[i] * cone_z[of_cluster[i]]
                 + s_ind[i] * rng.gauss(0.0, 1.0))
            if v > best_v:
                best_i, best_v = i, v
        wins[best_i] += 1

    per_path = [w / samples for w in wins]
    return per_path, _by_cluster(per_path, clusters)


def _by_cluster(per_path: list[float], clusters: list[list[int]]) -> list[float]:
    return [sum(per_path[i] for i in members) for members in clusters]


def severity(slack_ns: float | None, period_ns: float | None) -> float:
    """How close this path is to the timing constraint, in [0, 1].

        0.0   a full period of headroom
        0.5   exactly at the constraint
        1.0   a full period over it

    Monotone in delay across the whole range, which both halves of the old
    behaviour got wrong in opposite directions. Gating on "is it violated"
    scored every path in a design that meets timing identically, so a design
    being pushed to a tighter period could not be ranked at all. Simply
    clipping arrival/period at 1.0 then scored every *violating* path
    identically, which loses the ordering exactly where a failing design
    needs it.
    """
    if slack_ns is None or not period_ns:
        return 0.0
    return min(1.0, max(0.0, (1.0 - slack_ns / abs(period_ns)) / 2.0))


# --------------------------------------------------------------------------
# value
# --------------------------------------------------------------------------


def _violation(f: PathFeatures) -> float:
    return max(0.0, -(f.slack_ns if f.slack_ns is not None else 0.0))


def skill_term(feats: list[PathFeatures], lib: Any) -> tuple[float, list[dict]]:
    """How much the library already knows about this cluster's shape.

    Invalid entries are excluded from the score but still returned, so the
    brief can warn the agent off a known dead end instead of letting it
    rediscover one.
    """
    if lib is None:
        return 0.0, []
    patterns = []
    for f in feats:
        for fi in f.diagnosis.get("findings") or []:
            if fi.get("pattern") and fi["pattern"] != "slack gap":
                patterns.append(fi["pattern"])
    if not patterns:
        return 0.0, []
    matched = lib.match(patterns)
    best = 0.0
    for e in matched:
        if e.get("status") == "invalid":
            continue
        best = max(best, 0.5 * float(e.get("match_score") or 0.0)
                   + 0.5 * float(e.get("confidence") or 0.0))
    return best, matched


def cluster_value(feats: list[PathFeatures], pool: list[PathFeatures],
                  period_ns: float | None, lib: Any = None,
                  tns_total: float | None = None,
                  p_crit: float | None = None) -> dict[str, Any]:
    """Grounded worth of attacking one cluster. Every term lands in [0, 1].

    Three terms, and none of them assumes the design is failing:

      impact        P(this cluster holds the path that limits the clock).
                    Well-posed whether or not anything is violated, which is
                    the case that matters when the goal is a tighter period.
      severity      how much of the cycle its worst path consumes.
      tractability  whether anything can actually be done about it.
    """
    rep = min(feats, key=lambda f: (f.slack_ns if f.slack_ns is not None else 0.0))

    pool_viol = sum(_violation(f) for f in pool)
    own_viol = sum(_violation(f) for f in feats)
    truncated = False
    if tns_total is not None and abs(tns_total) > 1e-12 and pool_viol > 0:
        # The pool is one path per endpoint capped at --npaths. When the design
        # has more violating endpoints than that, the pool sum understates the
        # denominator and every share comes out inflated -- so normalise
        # against the reported TNS instead and say that is what happened.
        truncated = pool_viol < abs(tns_total) * 0.999
        mass = own_viol / abs(tns_total) if truncated else own_viol / pool_viol
    elif pool_viol > 0:
        mass = own_viol / pool_viol
    else:
        # Nothing violates. Rank by how little headroom is left instead, so a
        # design that already meets timing still orders its cones sensibly.
        slacks = [f.slack_ns for f in pool if f.slack_ns is not None]
        if slacks and max(slacks) - min(slacks) > 1e-12:
            hi = max(slacks)
            denom = sum(hi - s for s in slacks)
            mass = sum(hi - (f.slack_ns or 0.0) for f in feats) / denom if denom else 0.0
        else:
            mass = len(feats) / len(pool) if pool else 0.0

    # Impact is the criticality probability when it has been computed. The
    # TNS share is kept alongside it as reported evidence -- it is meaningful
    # only while something is violated, so it cannot be the primary term.
    impact = mass if p_crit is None else p_crit
    # The representative path's OWN clock, not the design's primary one.
    sev = severity(rep.slack_ns, rep.period_ns
                   if rep.period_ns is not None
                   else period_book(period_ns).primary_period)

    structural = [fi for fi in (rep.diagnosis.get("findings") or [])
                  if fi.get("pattern") != "slack gap"]
    skill, matched = skill_term(feats, lib)
    tract = (0.50 * rep.coverage
             + 0.30 * min(1.0, len(structural) / 2.0)
             + 0.20 * skill)

    w = VALUE_WEIGHTS
    return {
        "value": (w["mass"] * impact + w["criticality"] * sev
                  + w["tractability"] * tract),
        "terms": {"impact": impact, "severity": sev, "tractability": tract},
        "p_critical": p_crit,
        "mass_ns": own_viol,
        "tns_share": mass,
        "tns_share_truncated": truncated,
        "skills": matched,
        "representative": rep,
    }


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------


def select_clusters(clusters: list[list[int]], feats: list[PathFeatures],
                    sim: list[list[float]], period_ns: float | None,
                    k: int, lam: float = MMR_LAMBDA, lib: Any = None,
                    tns_total: float | None = None,
                    p_clusters: list[float] | None = None) -> list[dict[str, Any]]:
    """Maximal Marginal Relevance over the clusters.

        argmax [ (1 - lam) * value(C) - lam * max_{S selected} sim(C, S) ]

    Value and similarity are both in [0, 1], so subtracting one from the other
    is meaningful rather than a scale accident. Picking purely by value gets
    the same cone twice on any design with a wide register bank; picking purely
    by difference gets an irrelevant path that happens to look unusual.
    """
    scored = []
    for idx, c in enumerate(clusters):
        members = [feats[i] for i in c]
        pc = p_clusters[idx] if p_clusters else None
        v = cluster_value(members, feats, period_ns, lib, tns_total, pc)
        if v["terms"]["impact"] >= MASS_FLOOR:
            scored.append({"members": c, **v})
    if not scored:
        return []

    scored.sort(key=lambda d: (-d["value"], min(d["members"])))
    selected = [scored[0]]
    selected[0]["similarity_to_selected"] = 0.0
    remaining = scored[1:]

    while len(selected) < k and remaining:
        best, best_mmr, best_sim = None, None, 0.0
        for cand in remaining:
            worst_sim = max(cluster_similarity(cand["members"], s["members"], sim)
                            for s in selected)
            mmr = (1.0 - lam) * cand["value"] - lam * worst_sim
            if best_mmr is None or mmr > best_mmr:
                best, best_mmr, best_sim = cand, mmr, worst_sim
        if best is None or best_mmr <= 0.0:
            break
        best["similarity_to_selected"] = best_sim
        selected.append(best)
        remaining.remove(best)
    return selected


# --------------------------------------------------------------------------
# segmentation
# --------------------------------------------------------------------------


def _span_families(stages: list[dict[str, Any]]) -> dict[str, float]:
    return _normalise(Counter(rtl_map._family(s.get("cell"))
                              for s in stages if s.get("cell")))


def _span_delay(stages: list[dict[str, Any]]) -> float:
    return sum(float(s.get("delay") or 0.0) for s in stages)


def _cell_count(stages: list[dict[str, Any]]) -> int:
    """Combinational cells only.

    A launch or capture flop is not logic a rewrite can restructure, so a span
    holding two flops and two gates is not an attackable target however pure
    its cell mix looks. Counting flops let exactly that sliver be cut off the
    start of the reference design's path.
    """
    return sum(1 for s in stages
               if s.get("cell") and rtl_map._family(s.get("cell")) != "flop")


def split_points(stages: list[dict[str, Any]], k: int) -> list[tuple[int, int]]:
    """Cut an ordered stage list into at most k contiguous spans.

    Greedy binary splitting on cell-family divergence: repeatedly cut the span
    whose best available split separates the two most different cell mixes.
    A path is not homogeneous -- a partial-product AND array, a carry region
    and a final reduction have visibly different signatures -- and those
    boundaries are where one rewrite stops mattering and another starts.

    The floors are what keep a segment attackable: a two-cell span with 3% of
    the delay is a boundary artifact, not a target.
    """
    total = _span_delay(stages)
    if k <= 1 or total <= 0 or len(stages) < 2 * SEG_MIN_CELLS:
        return [(0, len(stages))]
    min_share = SEG_SHARE_OF_EVEN / k

    spans = [(0, len(stages))]
    while len(spans) < k:
        best_gain, best_choice = 0.0, None
        for si, (lo, hi) in enumerate(spans):
            for cut in range(lo + 1, hi):
                left, right = stages[lo:cut], stages[cut:hi]
                if _cell_count(left) < SEG_MIN_CELLS or _cell_count(right) < SEG_MIN_CELLS:
                    continue
                if (_span_delay(left) / total < min_share
                        or _span_delay(right) / total < min_share):
                    continue
                fl, fr = _span_families(left), _span_families(right)
                # Divergence says where a boundary is; the smaller half's share
                # of the WHOLE path says whether this span is the one worth
                # cutting. Both are needed. Purity alone splits whichever span
                # has the sharpest edge even when it owns 18% of the delay,
                # leaving the 64% span whole -- measured on the reference
                # design, where the carry region has no sharp internal edge to
                # find but is far too large to leave as one target.
                gain = (1.0 - hist_similarity(fl, fr)) * min(
                    _span_delay(left), _span_delay(right)) / total
                if gain > best_gain:
                    best_gain, best_choice = gain, (si, cut)
        if best_choice is None:
            break
        si, cut = best_choice
        lo, hi = spans.pop(si)
        spans[si:si] = [(lo, cut), (cut, hi)]
    return sorted(spans)


# --------------------------------------------------------------------------
# targets
# --------------------------------------------------------------------------


@dataclass
class PathTarget:
    id: str
    kind: str                      # "cone" | "segment"
    rank: int
    slack_ns: float | None
    value: float
    value_terms: dict[str, float]
    p_critical: float | None
    tns_share: float
    tns_share_truncated: bool
    mass_ns: float
    similarity_to_selected: float
    representative: dict[str, Any]
    member_count: int
    members: list[dict[str, Any]]
    stage_span: tuple[int, int] | None
    delay_ns: float
    delay_share: float
    cell_families: dict[str, int]
    cell_walk: list[str]
    max_fanout: int
    fanout_pins: list[str]
    regions: list[dict[str, Any]]
    signals: list[str]
    findings: list[dict[str, Any]]
    skills: list[dict[str, Any]]
    allowed_lines: dict[str, list[tuple[int, int]]]
    scope_enforceable: bool
    # The clock that captures this target's representative path, and its
    # period. Carried on the target so the brief handed to an agent states the
    # cycle the path actually has to fit in, not the design's primary one.
    clock_group: str | None = None
    period_ns: float | None = None
    brief: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["representative"] = {
            k: v for k, v in self.representative.items() if k != "stages"}
        d["allowed_lines"] = {f: [list(s) for s in sp]
                              for f, sp in self.allowed_lines.items()}
        d["stage_span"] = list(self.stage_span) if self.stage_span else None
        return d


def _scope(regions: list[dict[str, Any]], rtl: rtl_map.RtlIndex
           ) -> tuple[dict[str, list[tuple[int, int]]], bool]:
    """Line ranges an agent assigned this target may edit.

    Advisory only, and honest about it. Where yosys autoname collapses a whole
    cone onto its destination net -- measured at 87 of 91 stages on the
    reference design -- every segment of that path resolves to the same line
    and the scope stops discriminating. `scope_enforceable` says which case
    this is, and nothing about the correctness of a merge depends on it: the
    merge stage detects conflicts from the real diffs, not from this guess.
    """
    by_file: dict[str, list[tuple[int, int]]] = {}
    for r in regions:
        f = r.get("file")
        if not f:
            continue
        lo = max(1, int(r.get("line") or 1) - SCOPE_SLACK)
        hi = int(r.get("end_line") or r.get("line") or 1) + SCOPE_SLACK
        by_file.setdefault(f, []).append((lo, hi))
    merged: dict[str, list[tuple[int, int]]] = {}
    for f, spans in by_file.items():
        spans.sort()
        out: list[tuple[int, int]] = []
        for lo, hi in spans:
            if out and lo <= out[-1][1] + 1:
                out[-1] = (out[-1][0], max(out[-1][1], hi))
            else:
                out.append((lo, hi))
        merged[f] = out
    covered = sum(hi - lo + 1 for spans in merged.values() for lo, hi in spans)
    total = sum(len(lines) for lines in rtl.files.values()) or 1
    # Scope that covers most of the design is not scope.
    return merged, bool(merged) and covered < 0.6 * total


def _span_regions(stages: list[dict[str, Any]], nl: dict[str, Any],
                  rtl: rtl_map.RtlIndex) -> tuple[list[dict[str, Any]], list[str]]:
    """RTL regions and identifiers the stages in one span resolve to."""
    seen: dict[tuple, dict[str, Any]] = {}
    signals: list[str] = []
    for st in stages:
        inst, _ = rtl_map.split_pin(st.get("pin") or "")
        found = rtl_map.resolve(inst, nl, rtl)
        if not found:
            continue
        if not str(found[0].get("via", "")).startswith("src attribute"):
            found = found[:1]
        for r in found:
            key = rtl_map._region_key(r)
            slot = seen.setdefault(key, {
                "file": r.get("file"), "line": r.get("line"),
                "end_line": r.get("end_line"), "via": r.get("via"),
                "stages": 0, "delay_ns": 0.0,
            })
            slot["stages"] += 1
            slot["delay_ns"] += float(st.get("delay") or 0.0)
        ident = rtl_map.base_ident(rtl_map.strip_autoname(inst))
        if ident and not rtl_map.is_mangled(ident) and ident not in signals:
            signals.append(ident)
    out = sorted(seen.values(), key=lambda r: -r["delay_ns"])
    for r in out:
        r["delay_ns"] = round(r["delay_ns"], 5)
        r["source"] = rtl.span(r["file"], r["line"], r["end_line"])
    return out, signals


def _make_target(tid: str, kind: str, rank: int, sel: dict[str, Any],
                 span: tuple[int, int] | None, nl: dict[str, Any],
                 rtl: rtl_map.RtlIndex, period_ns: float | None,
                 feats: list[PathFeatures], lib: Any) -> PathTarget:
    rep_f: PathFeatures = sel["representative"]
    path = sel["_paths"][rep_f.index]
    stages = path.get("stages") or []
    lo, hi = span if span else (0, len(stages))
    sub = stages[lo:hi]

    total_delay = _span_delay(stages) or 1e-12
    if span is None:
        regions, signals = rep_f.mapping.get("regions") or [], []
        for r in regions:
            for s in r.get("signals") or []:
                if s not in signals:
                    signals.append(s)
        diagnosis = rep_f.diagnosis
    else:
        regions, signals = _span_regions(sub, nl, rtl)
        # Diagnose the span on its own terms. slack_ns is withheld so the
        # "slack gap" finding -- which is a property of the whole path, not of
        # any one segment of it -- does not fire once per segment.
        diagnosis = rtl_map.diagnose(
            {"stages": sub, "slack_ns": None,
             "logic_depth": _cell_count(sub)}, rep_f.period_ns)

    fanouts = [(int(s["fanout"]), s.get("pin") or "")
               for s in sub if s.get("fanout") is not None]
    fanouts.sort(key=lambda t: -t[0])

    allowed, enforceable = _scope(regions, rtl)
    findings = [f for f in (diagnosis.get("findings") or [])
                if f.get("pattern") != "slack gap"]
    skills = sel.get("skills") or []
    if lib is not None and span is not None and findings:
        skills = lib.match([f["pattern"] for f in findings])

    value, terms = sel["value"], sel["terms"]
    share, mass_ns = sel["tns_share"], sel["mass_ns"]
    if span is not None:
        # A segment inherits its cone's criticality -- it sits on the same
        # failing path -- but not its mass or its tractability. Those have to
        # be the segment's own, or three cuts of one path all report the same
        # number and the portfolio cannot order them.
        delay_share = _span_delay(sub) / total_delay
        seg_skill = 0.0
        for e in skills:
            if e.get("status") == "invalid":
                continue
            seg_skill = max(seg_skill, 0.5 * float(e.get("match_score") or 0.0)
                            + 0.5 * float(e.get("confidence") or 0.0))
        seg_cov = (sum(1 for st in sub
                       if rtl_map.resolve(rtl_map.split_pin(st.get("pin") or "")[0],
                                          nl, rtl)) / len(sub)) if sub else 0.0
        terms = {"impact": sel["terms"]["impact"] * delay_share,
                 "severity": sel["terms"]["severity"],
                 "tractability": (0.50 * seg_cov
                                  + 0.30 * min(1.0, len(findings) / 2.0)
                                  + 0.20 * seg_skill)}
        w = VALUE_WEIGHTS
        value = (w["mass"] * terms["impact"] + w["criticality"] * terms["severity"]
                 + w["tractability"] * terms["tractability"])
        # Share of the *design's* failure attributable to this span: the cone
        # owns `sel["tns_share"]` of it, and this span owns `delay_share` of
        # the cone's path delay.
        share = sel["tns_share"] * delay_share
        mass_ns = sel["mass_ns"] * delay_share

    t = PathTarget(
        id=tid, kind=kind, rank=rank,
        slack_ns=rep_f.slack_ns,
        value=value, value_terms=terms,
        p_critical=sel.get("p_critical"),
        tns_share=share,
        tns_share_truncated=sel["tns_share_truncated"],
        mass_ns=mass_ns,
        similarity_to_selected=sel.get("similarity_to_selected", 0.0),
        representative=path,
        member_count=len(sel["members"]),
        members=[{"slack_ns": feats[i].slack_ns, "endpoint": feats[i].endpoint,
                  "startpoint": feats[i].startpoint} for i in sel["members"][:12]],
        stage_span=span,
        delay_ns=round(_span_delay(sub), 5),
        delay_share=round(_span_delay(sub) / total_delay, 4),
        cell_families=dict(Counter(rtl_map._family(s.get("cell"))
                                   for s in sub if s.get("cell")).most_common()),
        cell_walk=[s["cell"] for s in sub if s.get("cell")],
        max_fanout=fanouts[0][0] if fanouts else 0,
        fanout_pins=[p for fo, p in fanouts[:4] if fo >= rtl_map.HIGH_FANOUT],
        regions=regions, signals=signals[:12],
        findings=findings, skills=skills,
        allowed_lines=allowed, scope_enforceable=enforceable,
        clock_group=rep_f.path_group, period_ns=rep_f.period_ns,
    )
    t.brief = render_brief(t)
    return t


def build_targets(timing: dict[str, Any], nl: dict[str, Any],
                  rtl: rtl_map.RtlIndex, period_ns: Any = None,
                  k: int = 3, lam: float = MMR_LAMBDA,
                  cluster_at: float = CLUSTER_AT,
                  lib: Any = None, sigma: float = DELAY_SIGMA,
                  rho: float = CONE_RHO) -> dict[str, Any]:
    """The portfolio: at most k distinct targets, plus how they were chosen.

    ``period_ns`` accepts a ``clocks.ClockSet`` or a bare float. With a
    ClockSet every path is normalised against the clock that captured it.
    """
    period_ns = period_book(period_ns)
    paths = [p for p in (timing.get("critical_paths") or []) if p.get("stages")]
    if not paths:
        return {"targets": [], "k_requested": k, "k_effective": 0,
                "clusters": [], "collapsed": False, "distribution": [],
                "pool": {"paths": 0, "violating": 0}}

    feats = [path_features(p, i, nl, rtl, period_ns) for i, p in enumerate(paths)]
    clusters, sim = cluster(feats, cluster_at)
    tns_total = (timing.get("summary") or {}).get("tns_ns")
    p_paths, p_clusters = criticality(feats, clusters, period_ns, sigma, rho)
    picked = select_clusters(clusters, feats, sim, period_ns, k, lam, lib,
                             tns_total, p_clusters)
    for p in picked:
        p["_paths"] = paths

    targets: list[PathTarget] = []
    for rank, sel in enumerate(picked, start=1):
        targets.append(_make_target(f"T{rank}", "cone", rank, sel, None,
                                    nl, rtl, period_ns, feats, lib))

    # Fewer cones than agents: cut the best cone's representative into
    # contiguous segments instead. This is the measured common case, not an
    # edge case -- a small design usually has exactly one bottleneck cone.
    collapsed = False
    if targets and len(targets) < k:
        top = picked[0]
        rep_path = paths[top["representative"].index]
        spans = split_points(rep_path.get("stages") or [], k - len(targets) + 1)
        if len(spans) > 1:
            collapsed = True
            targets = [t for t in targets if t.rank != 1]
            seg_targets = [
                _make_target(f"T{i}", "segment", i, top, span,
                             nl, rtl, period_ns, feats, lib)
                for i, span in enumerate(spans, start=1)
            ]
            targets = seg_targets + targets
            for rank, t in enumerate(targets, start=1):
                t.id, t.rank = f"T{rank}", rank
                t.brief = render_brief(t)

    violating = sum(1 for f in feats if (f.slack_ns or 0.0) < 0)
    return {
        "targets": targets,
        "k_requested": k,
        "k_effective": len(targets),
        "collapsed": collapsed,
        "clusters": [{"members": c["members"], "value": c["value"],
                      "terms": c["terms"], "tns_share": c["tns_share"],
                      "p_critical": c.get("p_critical")}
                     for c in picked],
        "cluster_count": len(clusters),
        "distribution": sorted(
            ({"rank": i + 1, "p_critical": p_paths[i],
              "slack_ns": feats[i].slack_ns, "status": feats[i].status,
              "startpoint": feats[i].startpoint, "endpoint": feats[i].endpoint,
              "cone": next(ci for ci, mem in enumerate(clusters) if i in mem)}
             for i in range(len(feats))),
            key=lambda d: -d["p_critical"]),
        "cone_distribution": [
            {"cone": ci, "p_critical": p_clusters[ci], "paths": len(mem),
             "worst_slack_ns": min((feats[i].slack_ns for i in mem
                                    if feats[i].slack_ns is not None),
                                   default=None)}
            for ci, mem in enumerate(clusters)],
        "sigma": sigma, "rho": rho,
        "clocks": _clock_coverage(feats),
        "pool": {"paths": len(paths), "violating": violating,
                 "tns_ns": tns_total,
                 "truncated": any(t.tns_share_truncated for t in targets)},
    }


def _clock_coverage(feats: list[PathFeatures]) -> dict[str, Any]:
    """Which clock each path was normalised against, and whether that was a
    lookup or a fallback.

    A fallback on a multi-clock design means some path was ranked against a
    period that is not its own -- the exact failure mode section 6.1 of
    HANDOFF.md calls the highest-risk item in the change. It is reported rather
    than swallowed, so a wrong ranking is visible in the artifact instead of
    only in the conclusion drawn from it.
    """
    seen: dict[str, dict[str, Any]] = {}
    for f in feats:
        key = f.path_group or "(unnamed)"
        e = seen.setdefault(key, {"paths": 0, "period_ns": f.period_ns,
                                  "resolved": f.period_exact})
        e["paths"] += 1
    unresolved = sorted(g for g, e in seen.items() if not e["resolved"])
    return {"groups": seen, "unresolved": unresolved,
            "all_resolved": not unresolved}


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def _walk(cells: list[str], limit: int = 40) -> str:
    if len(cells) <= limit:
        return " -> ".join(cells)
    head, tail = cells[: limit // 2], cells[-(limit // 2):]
    return (" -> ".join(head) + f"  ... {len(cells) - limit} more ...  "
            + " -> ".join(tail))


def render_brief(t: PathTarget, period_ns: float | None = None) -> str:
    """The focus brief one specialist agent receives in place of the whole report.

    ``period_ns`` defaults to the target's own clock, which is the cycle the
    path actually has to fit in. Passing it explicitly overrides that.
    """
    period_ns = period_ns if period_ns is not None else t.period_ns
    L: list[str] = []
    violating = t.slack_ns is not None and t.slack_ns < 0
    if violating:
        share = f"{t.tns_share:.0%}" + (" of reported TNS" if t.tns_share_truncated
                                        else " of the design's timing failure")
    else:
        share = (f"{t.p_critical:.0%} likely to be what limits the clock"
                 if t.p_critical is not None else "no violation")
    L.append(f"## Target {t.id} -- {t.kind}, {share}")
    L.append("")
    p = t.representative
    slack = t.slack_ns
    if slack is None or not period_ns:
        frac = ""
    elif slack < 0:
        frac = f" ({abs(slack) / period_ns:.0%} short of a {period_ns:g} ns cycle)"
    else:
        frac = (f" (meets timing; the path uses "
                f"{severity(slack, period_ns):.0%} of a {period_ns:g} ns cycle)")
    L.append(f"representative  {p.get('startpoint')} -> {p.get('endpoint')}")
    L.append(f"slack           {slack} ns{frac}")
    if t.clock_group:
        L.append(f"clock           {t.clock_group}"
                 + (f", {t.period_ns:g} ns period" if t.period_ns else ""))
    if t.kind == "cone":
        L.append(f"cluster         {t.member_count} path(s) through the same cone")
    else:
        lo, hi = t.stage_span or (0, 0)
        L.append(f"segment         stages {lo}-{hi} of "
                 f"{len(p.get('stages') or [])} on the worst path")
        L.append(f"                {t.delay_ns:.4f} ns, {t.delay_share:.0%} "
                 f"of the path's delay")
    if t.p_critical is not None:
        what = ("the path this segment sits on limits" if t.kind == "segment"
                else "this cone limits")
        L.append(f"criticality     {what} the clock {t.p_critical:.0%} of the "
                 f"time under the delay-uncertainty model")
    L.append(f"value           {t.value:.3f}  (impact {t.value_terms['impact']:.2f}, "
             f"severity {t.value_terms['severity']:.2f}, "
             f"tractability {t.value_terms['tractability']:.2f})")
    if t.similarity_to_selected:
        L.append(f"distinctness    similarity {t.similarity_to_selected:.2f} "
                 f"to the nearest other target")
    L.append("")

    if t.cell_families:
        L.append("cell mix        " + ", ".join(f"{n}x {f}" for f, n
                                                in t.cell_families.items()))
    if t.max_fanout >= rtl_map.HIGH_FANOUT:
        L.append(f"fanout peak     {t.max_fanout} at "
                 + ", ".join(t.fanout_pins[:2]))
    L.append("")

    L.append("root causes (mechanical, from the STA report):")
    for f in t.findings:
        L.append(f"  - {f['pattern']}: {f['evidence']}")
    if not t.findings:
        L.append("  - none of the structural heuristics fired on this span")
    L.append("")

    if t.cell_walk:
        L.append("cell walk in path order (this is the topology -- a run of")
        L.append("AOI/OAI/XOR is carry propagation, a run of AND/OR feeding a")
        L.append("wide reduction is comparison or selection logic):")
        L.append("  " + _walk(t.cell_walk))
        L.append("")

    L.append("RTL this target resolves to:")
    for r in t.regions[:6]:
        L.append(f"  {r['file']}:{r['line']}"
                 + (f"-{r['end_line']}" if r['end_line'] != r['line'] else "")
                 + f"  {r['stages']} stage(s), {r['delay_ns']:.4f} ns "
                   f"via {r['via']}")
        for src in (r.get("source") or [])[:6]:
            L.append(f"    {src}")
    if t.signals:
        L.append(f"  signals: {', '.join(t.signals[:10])}")
    if not t.scope_enforceable:
        L.append("  NOTE: synthesis named this whole cone after its destination")
        L.append("  net, so these line numbers are coarse. Use the cell walk and")
        L.append("  the signal list to decide what this target actually is.")
    L.append("")

    if t.skills:
        import skills as skills_mod
        L.append("skills the library offers for this pattern:")
        L.append(skills_mod.render(t.skills, with_examples=False))
    return "\n".join(L)


def render(sel: dict[str, Any]) -> str:
    L: list[str] = []
    pool = sel.get("pool") or {}
    L.append(f"path pool: {pool.get('paths', 0)} reported, "
             f"{pool.get('violating', 0)} violating, "
             f"{sel.get('cluster_count', 0)} distinct cone(s)")
    L.append(f"portfolio: k_effective {sel['k_effective']} of "
             f"{sel['k_requested']} requested"
             + ("  [one cone -- cut into segments]" if sel.get("collapsed") else ""))
    if pool.get("truncated"):
        L.append("NOTE: the reported path pool does not cover every violating "
                 "endpoint; shares are normalised against reported TNS.")
    L.append("")

    cones = sel.get("cone_distribution") or []
    if cones:
        L.append(f"Which cone limits the clock (delay sigma "
                 f"{sel.get('sigma', 0):.0%} of the period, "
                 f"within-cone correlation {sel.get('rho', 0):.2f}):")
        for c in sorted(cones, key=lambda d: -d["p_critical"]):
            if c["p_critical"] < 0.001:
                continue
            L.append(f"  cone {c['cone']}   {c['p_critical']:6.1%}   "
                     f"{c['paths']:3d} path(s)   worst slack "
                     f"{c['worst_slack_ns']}")
        L.append("")
    dist = [d for d in (sel.get("distribution") or []) if d["p_critical"] >= 0.005]
    if dist:
        L.append("Per path, most likely first:")
        for d in dist[:8]:
            L.append(f"  {d['p_critical']:6.1%}  slack {d['slack_ns']:>9}  "
                     f"({d['status']})  cone {d['cone']}  -> {d['endpoint']}")
        L.append("")
    for t in sel.get("targets", []):
        L.append(t.brief)
        L.append("")
    return "\n".join(L)


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------


def from_run(rdir: Path, design_dir: Path | None = None,
             period_ns: float | None = None, k: int = 3,
             stage: str = "sta", lam: float = MMR_LAMBDA,
             cluster_at: float = CLUSTER_AT, lib: Any = None,
             sigma: float = DELAY_SIGMA, rho: float = CONE_RHO) -> dict[str, Any]:
    """Build the portfolio from a finished run directory."""
    if stage not in _STAGE_DIRS:
        raise ValueError(f"unknown stage {stage!r}; expected one of "
                         f"{', '.join(sorted(_STAGE_DIRS))}")
    tjson = rdir / _STAGE_DIRS[stage] / "timing.json"
    if not tjson.is_file():
        raise FileNotFoundError(
            f"no {_STAGE_DIRS[stage]}/timing.json in {rdir.name}"
            + ("  Run: astra run <design> --pnr" if stage == "pnr" else ""))
    timing = json.loads(tjson.read_text())
    # The netlist JSON only exists post-synthesis; post-PnR paths are still
    # localised against it, since it is the only structural source there is.
    nl = rtl_map.load_netlist_index(rdir / "01_synth" / "netlist.json")
    srcs = sorted((rdir / "00_inputs").glob("*.v")) + \
        sorted((rdir / "00_inputs").glob("*.sv"))
    if not srcs and design_dir:
        srcs = sorted((design_dir / "rtl").glob("*.v"))
    if period_ns is None:
        m = rdir / "metrics.json"
        if m.is_file():
            metrics = json.loads(m.read_text())
            # The whole clock set, so each path is ranked against its own
            # clock. Falls back to the primary period only if the run predates
            # multi-clock metrics.
            period_ns = clocks.from_metrics(metrics)
            if period_ns is None:
                period_ns = (metrics.get("clock") or {}).get("period_ns")
    return build_targets(timing, nl, rtl_map.RtlIndex(srcs), period_ns,
                         k, lam, cluster_at, lib, sigma, rho)


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="astra-paths",
        description="Select the distinct critical-path targets worth an agent call.")
    ap.add_argument("target", help="a run directory, or a design name with --run")
    ap.add_argument("--run", default="latest")
    ap.add_argument("--stage", choices=sorted(_STAGE_DIRS), default="sta")
    ap.add_argument("-k", "--top-k", type=int, default=3)
    ap.add_argument("--mmr-lambda", type=float, default=MMR_LAMBDA)
    ap.add_argument("--cluster-at", type=float, default=CLUSTER_AT)
    ap.add_argument("--delay-sigma", type=float, default=DELAY_SIGMA,
                    help="delay uncertainty as a fraction of the clock period; "
                         "0 gives the deterministic answer")
    ap.add_argument("--cone-rho", type=float, default=CONE_RHO,
                    help="share of that uncertainty common to a whole cone")
    ap.add_argument("--no-skills", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv[1:])

    p = Path(args.target)
    design_dir = None
    if p.is_dir() and (p / "02_sta").exists() or (p / "metrics.json").is_file():
        rdir = p
    else:
        import astra
        rdir = astra.find_run(args.target, args.run)
        design_dir = astra.DESIGNS / args.target

    lib = None
    if not args.no_skills:
        import skills as skills_mod
        lib = skills_mod.SkillLibrary()

    try:
        sel = from_run(rdir, design_dir, k=args.top_k, stage=args.stage,
                       lam=args.mmr_lambda, cluster_at=args.cluster_at, lib=lib,
                       sigma=args.delay_sigma, rho=args.cone_rho)
    except (FileNotFoundError, ValueError) as e:
        print(f"[pathsel] ERROR: {e}", file=sys.stderr)
        return 1

    if args.json:
        out = dict(sel)
        out["targets"] = [t.to_dict() for t in sel["targets"]]
        print(json.dumps(out, indent=2, default=str))
    else:
        print(render(sel))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
