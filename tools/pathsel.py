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
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
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
VALUE_WEIGHTS: dict[str, float] = {"mass": 0.45, "criticality": 0.35,
                                   "tractability": 0.20}

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
    mapping: dict[str, Any] = field(default_factory=dict, repr=False)
    diagnosis: dict[str, Any] = field(default_factory=dict, repr=False)


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
                  rtl: rtl_map.RtlIndex, period_ns: float | None) -> PathFeatures:
    mapping = rtl_map.map_path(path, nl, rtl)
    diagnosis = rtl_map.diagnose(path, period_ns)
    return PathFeatures(
        index=index,
        slack_ns=path.get("slack_ns"),
        status=path.get("status"),
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
                  tns_total: float | None = None) -> dict[str, Any]:
    """Grounded worth of attacking one cluster. Every term lands in [0, 1]."""
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

    worst = rep.slack_ns
    crit = 0.0
    if worst is not None and worst < 0 and period_ns:
        crit = min(1.0, abs(worst) / abs(period_ns))

    structural = [fi for fi in (rep.diagnosis.get("findings") or [])
                  if fi.get("pattern") != "slack gap"]
    skill, matched = skill_term(feats, lib)
    tract = (0.50 * rep.coverage
             + 0.30 * min(1.0, len(structural) / 2.0)
             + 0.20 * skill)

    w = VALUE_WEIGHTS
    return {
        "value": w["mass"] * mass + w["criticality"] * crit + w["tractability"] * tract,
        "terms": {"mass": mass, "criticality": crit, "tractability": tract},
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
                    tns_total: float | None = None) -> list[dict[str, Any]]:
    """Maximal Marginal Relevance over the clusters.

        argmax [ (1 - lam) * value(C) - lam * max_{S selected} sim(C, S) ]

    Value and similarity are both in [0, 1], so subtracting one from the other
    is meaningful rather than a scale accident. Picking purely by value gets
    the same cone twice on any design with a wide register bank; picking purely
    by difference gets an irrelevant path that happens to look unusual.
    """
    scored = []
    for c in clusters:
        members = [feats[i] for i in c]
        v = cluster_value(members, feats, period_ns, lib, tns_total)
        if v["terms"]["mass"] >= MASS_FLOOR:
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
             "logic_depth": _cell_count(sub)}, period_ns)

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
        terms = {"mass": delay_share,
                 "criticality": sel["terms"]["criticality"],
                 "tractability": (0.50 * seg_cov
                                  + 0.30 * min(1.0, len(findings) / 2.0)
                                  + 0.20 * seg_skill)}
        w = VALUE_WEIGHTS
        value = (w["mass"] * terms["mass"] + w["criticality"] * terms["criticality"]
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
    )
    t.brief = render_brief(t, period_ns)
    return t


def build_targets(timing: dict[str, Any], nl: dict[str, Any],
                  rtl: rtl_map.RtlIndex, period_ns: float | None = None,
                  k: int = 3, lam: float = MMR_LAMBDA,
                  cluster_at: float = CLUSTER_AT,
                  lib: Any = None) -> dict[str, Any]:
    """The portfolio: at most k distinct targets, plus how they were chosen."""
    paths = [p for p in (timing.get("critical_paths") or []) if p.get("stages")]
    if not paths:
        return {"targets": [], "k_requested": k, "k_effective": 0,
                "clusters": [], "collapsed": False,
                "pool": {"paths": 0, "violating": 0}}

    feats = [path_features(p, i, nl, rtl, period_ns) for i, p in enumerate(paths)]
    clusters, sim = cluster(feats, cluster_at)
    tns_total = (timing.get("summary") or {}).get("tns_ns")
    picked = select_clusters(clusters, feats, sim, period_ns, k, lam, lib, tns_total)
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
                t.brief = render_brief(t, period_ns)

    violating = sum(1 for f in feats if (f.slack_ns or 0.0) < 0)
    return {
        "targets": targets,
        "k_requested": k,
        "k_effective": len(targets),
        "collapsed": collapsed,
        "clusters": [{"members": c["members"], "value": c["value"],
                      "terms": c["terms"], "tns_share": c["tns_share"]}
                     for c in picked],
        "cluster_count": len(clusters),
        "pool": {"paths": len(paths), "violating": violating,
                 "tns_ns": tns_total,
                 "truncated": any(t.tns_share_truncated for t in targets)},
    }


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
    """The focus brief one specialist agent receives in place of the whole report."""
    L: list[str] = []
    share = f"{t.tns_share:.0%}" + (" (of reported TNS)" if t.tns_share_truncated else "")
    L.append(f"## Target {t.id} -- {t.kind}, {share} of the design's timing failure")
    L.append("")
    p = t.representative
    slack = t.slack_ns
    frac = (f" ({abs(slack) / period_ns:.0%} of a {period_ns:g} ns cycle)"
            if slack is not None and slack < 0 and period_ns else "")
    L.append(f"representative  {p.get('startpoint')} -> {p.get('endpoint')}")
    L.append(f"slack           {slack} ns{frac}")
    if t.kind == "cone":
        L.append(f"cluster         {t.member_count} path(s) through the same cone")
    else:
        lo, hi = t.stage_span or (0, 0)
        L.append(f"segment         stages {lo}-{hi} of "
                 f"{len(p.get('stages') or [])} on the worst path")
        L.append(f"                {t.delay_ns:.4f} ns, {t.delay_share:.0%} "
                 f"of the path's delay")
    L.append(f"value           {t.value:.3f}  (mass {t.value_terms['mass']:.2f}, "
             f"criticality {t.value_terms['criticality']:.2f}, "
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
             cluster_at: float = CLUSTER_AT, lib: Any = None) -> dict[str, Any]:
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
            period_ns = (json.loads(m.read_text()).get("clock") or {}).get("period_ns")
    return build_targets(timing, nl, rtl_map.RtlIndex(srcs), period_ns,
                         k, lam, cluster_at, lib)


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
                       lam=args.mmr_lambda, cluster_at=args.cluster_at, lib=lib)
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
