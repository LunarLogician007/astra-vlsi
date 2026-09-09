#!/usr/bin/env python3
"""Mechanical union of several RTL rewrites, and the conflict report.

Three agents each fix a different part of one design. Two questions follow, and
only the second one needs a model:

  * Do their edits touch disjoint text? -- pure diff arithmetic, answered here.
  * When they do not, which fix wins? -- judgement, answered by the merge agent.

So this module builds every union that can be assembled by splicing text, and
those become candidates evaluated exactly like a model's output. They cost
nothing, and they are the control the merge agent has to beat: if the model's
union never scores better than the one `difflib` assembled for free, the merge
call is not paying for itself, and the trajectory will show that rather than
crediting the model for work a patch tool did.

Two properties are load-bearing and both are tested:

  Order independence.  The union of a set of candidates must not depend on the
  order they are applied in. A control whose result changes when you shuffle
  its inputs is not a control. Conflicts are therefore resolved by *dropping*
  an entire contested region rather than by letting the last writer win.

  Robustness to reformatting.  A model asked for a complete file may return it
  reflowed, which makes every line a hunk and every candidate conflict with
  every other. Such a candidate is detected by how much of the file it changed
  and excluded from the mechanical union with that reason recorded -- not
  silently dropped, and not allowed to poison the others.
"""

from __future__ import annotations

import difflib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# A candidate that rewrote more than this fraction of a file's lines is
# treated as a reformat, not a targeted edit. Chosen well above what a real
# transformation touches (a rebalanced adder tree is a handful of lines) and
# well below a wholesale reflow.
REWRITE_THRESHOLD = 0.60

# Hunks this close together are treated as touching: two edits three lines
# apart are almost certainly the same expression, and splicing both produces
# text neither candidate wrote.
GUARD = 1


@dataclass(frozen=True)
class Hunk:
    cand: str
    file: str
    tag: str          # "replace" | "insert" | "delete"
    p0: int           # parent-side line range, half-open, 0-based
    p1: int
    lines: tuple[str, ...]   # replacement text

    @property
    def span(self) -> tuple[int, int]:
        """Parent lines this hunk lays claim to.

        An insertion has an empty parent range, which would never overlap
        anything under a naive test -- so it claims the joint either side of
        the insertion point instead.
        """
        if self.p0 == self.p1:
            return (self.p0 - GUARD, self.p0 + GUARD)
        return (self.p0, self.p1)


# --------------------------------------------------------------------------
# diffing
# --------------------------------------------------------------------------


def normalise(text: str) -> list[str]:
    """Lines with trailing whitespace and runs of blank lines removed.

    Diffing raw text makes a candidate that changed indentation look like it
    rewrote the file. Normalising first keeps the comparison on what the code
    says rather than how it was typed.
    """
    out: list[str] = []
    blanks = 0
    for line in (text or "").splitlines():
        line = line.rstrip()
        if line:
            blanks = 0
        else:
            blanks += 1
            if blanks > 1:
                continue
        out.append(line)
    while out and not out[-1]:
        out.pop()
    return out


def hunks(parent: list[str], child: list[str], cand: str, fname: str) -> list[Hunk]:
    """Every non-equal opcode between two normalised files."""
    sm = difflib.SequenceMatcher(a=parent, b=child, autojunk=False)
    out: list[Hunk] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        out.append(Hunk(cand=cand, file=fname, tag=tag, p0=i1, p1=i2,
                        lines=tuple(child[j1:j2])))
    return out


def rewrite_fraction(parent: list[str], child: list[str]) -> float:
    """Share of the parent's lines a candidate replaced, deleted or added."""
    if not parent:
        return 1.0 if child else 0.0
    hs = hunks(parent, child, "_", "_")
    touched = sum(h.p1 - h.p0 for h in hs)
    inserted = sum(len(h.lines) for h in hs if h.tag == "insert")
    return min(1.0, (touched + inserted) / len(parent))


def unified(parent_text: str, child_text: str, fname: str, cand: str,
            context: int = 3) -> str:
    """A unified diff -- what the merge agent reads instead of a whole file."""
    return "\n".join(difflib.unified_diff(
        normalise(parent_text), normalise(child_text),
        fromfile=f"{fname} (parent)", tofile=f"{fname} ({cand})",
        n=context, lineterm=""))


# --------------------------------------------------------------------------
# conflicts
# --------------------------------------------------------------------------


def overlap(a: Hunk, b: Hunk) -> bool:
    if a.file != b.file:
        return False
    (a0, a1), (b0, b1) = a.span, b.span
    return a0 < b1 and b0 < a1


def components(all_hunks: Iterable[Hunk]) -> list[list[Hunk]]:
    """Connected components of the overlap graph, in parent-line order.

    Transitivity matters: A may not touch C, but if B touches both then all
    three describe one contested region and resolving any pair of them in
    isolation would produce text none of the candidates wrote.
    """
    items = sorted(all_hunks, key=lambda h: (h.file, h.p0, h.p1, h.cand))
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if overlap(items[i], items[j]):
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[max(ri, rj)] = min(ri, rj)

    groups: dict[int, list[Hunk]] = {}
    for i, h in enumerate(items):
        groups.setdefault(find(i), []).append(h)
    return [groups[k] for k in sorted(groups)]


def conflict_report(comps: list[list[Hunk]]) -> dict[str, Any]:
    """Which regions more than one candidate laid claim to."""
    conflicts, clean = [], []
    for comp in comps:
        cands = sorted({h.cand for h in comp})
        rec = {
            "file": comp[0].file,
            "candidates": cands,
            "parent_lines": [min(h.p0 for h in comp) + 1,
                             max(h.p1 for h in comp)],
            "hunks": len(comp),
        }
        (conflicts if len(cands) > 1 else clean).append(rec)
    return {"conflicts": conflicts, "clean": clean,
            "conflicted_candidates": sorted({c for r in conflicts
                                             for c in r["candidates"]})}


def render_conflicts(report: dict[str, Any], excluded: dict[str, str]) -> str:
    L: list[str] = []
    if excluded:
        L.append("Candidates excluded from the mechanical union:")
        for cand, why in sorted(excluded.items()):
            L.append(f"  {cand}: {why}")
        L.append("")
    if report["conflicts"]:
        L.append("Contested regions -- more than one candidate edited these, so "
                 "no mechanical union can include them:")
        for c in report["conflicts"]:
            L.append(f"  {c['file']}:{c['parent_lines'][0]}-{c['parent_lines'][1]}"
                     f"  claimed by {', '.join(c['candidates'])}")
    else:
        L.append("No contested regions: every candidate edited text no other "
                 "candidate touched, so their fixes compose by construction.")
    L.append("")
    L.append(f"{len(report['clean'])} uncontested edit region(s) across all "
             f"candidates.")
    return "\n".join(L)


# --------------------------------------------------------------------------
# union
# --------------------------------------------------------------------------


def _apply(parent: list[str], keep: list[Hunk]) -> list[str]:
    """Splice hunks into the parent, latest first so earlier offsets hold."""
    out = list(parent)
    for h in sorted(keep, key=lambda h: (-h.p0, -h.p1)):
        out[h.p0:h.p1] = list(h.lines)
    return out


def mechanical_union(parent_files: dict[str, str],
                     cand_files: dict[str, dict[str, str]],
                     ) -> tuple[dict[str, str], dict[str, Any]]:
    """Union of several candidates by text splicing. Order-independent.

    ``parent_files`` maps a file name to its text; ``cand_files`` maps a
    candidate id to the same shape. Returns the merged files and a report.

    Any region two or more candidates edited is dropped in full. Picking one of
    them would make the result depend on candidate order, which would make this
    a coin flip dressed as a control.
    """
    excluded: dict[str, str] = {}
    all_hunks: list[Hunk] = []

    for cand, files in sorted(cand_files.items()):
        for fname, parent_text in sorted(parent_files.items()):
            child_text = files.get(fname)
            if child_text is None:
                continue
            p, c = normalise(parent_text), normalise(child_text)
            frac = rewrite_fraction(p, c)
            if frac > REWRITE_THRESHOLD:
                excluded[cand] = (f"rewrote {frac:.0%} of {fname} -- a reformat, "
                                  f"not a targeted edit; it cannot be composed "
                                  f"hunk-wise")
                break
            all_hunks.extend(hunks(p, c, cand, fname))
        # A candidate excluded on one file is excluded entirely: half of a
        # rewrite is not a rewrite.
        if cand in excluded:
            all_hunks = [h for h in all_hunks if h.cand != cand]

    comps = components(all_hunks)
    report = conflict_report(comps)
    keep = [h for comp in comps if len({x.cand for x in comp}) == 1 for h in comp]

    merged: dict[str, str] = {}
    for fname, text in parent_files.items():
        p = normalise(text)
        mine = [h for h in keep if h.file == fname]
        merged[fname] = "\n".join(_apply(p, mine)) + "\n"

    applied = sorted({h.cand for h in keep})
    report.update({
        "excluded": excluded,
        "applied_candidates": applied,
        "applied_hunks": len(keep),
        "dropped_hunks": len(all_hunks) - len(keep),
        "changed": any(merged[f] != (("\n".join(normalise(t))) + "\n")
                       for f, t in parent_files.items()),
    })
    return merged, report


def subsets(cands: list[str], min_size: int = 2) -> list[tuple[str, ...]]:
    """Every combination of at least min_size candidates, largest first.

    With three specialists this is {1,2}, {1,3}, {2,3}, {1,2,3} -- four extra
    fully-evaluated candidates per iteration for no model calls at all.
    """
    out: list[tuple[str, ...]] = []
    n = len(cands)
    for mask in range(1, 1 << n):
        picked = tuple(cands[i] for i in range(n) if mask & (1 << i))
        if len(picked) >= min_size:
            out.append(picked)
    out.sort(key=lambda t: (-len(t), t))
    return out


def union_id(members: Iterable[str]) -> str:
    """`u12` from `('t1', 't2')` -- short enough to be a directory name."""
    digits = "".join(m[-1] if m and m[-1].isdigit() else m[:1] for m in members)
    return f"u{digits}"


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="astra-merge",
        description="Mechanically union several RTL rewrites of one parent.")
    ap.add_argument("parent", type=Path)
    ap.add_argument("candidates", type=Path, nargs="+",
                    help="one file per candidate, same module as the parent")
    ap.add_argument("-o", "--out", type=Path)
    args = ap.parse_args(argv[1:])

    name = args.parent.name
    parent = {name: args.parent.read_text()}
    cands = {f"c{i + 1}": {name: p.read_text()}
             for i, p in enumerate(args.candidates)}

    merged, report = mechanical_union(parent, cands)
    print(render_conflicts(report, report["excluded"]))
    print()
    print(f"applied: {', '.join(report['applied_candidates']) or 'nothing'}"
          f"  ({report['applied_hunks']} hunk(s), "
          f"{report['dropped_hunks']} dropped)")
    if args.out:
        args.out.write_text(merged[name])
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
