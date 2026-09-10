#!/usr/bin/env python3
"""Regions of RTL an optimiser may not touch, and the check that it didn't.

Some logic is invisible to the equivalence gate every candidate passes
through. Per docs/equivalence-contract.md the per-domain proof CUTS the clock
domain crossings, so on either side of that cut a change is unprovable rather
than merely unproven: removing a synchroniser stage, re-encoding a gray-coded
bus so two bits can change in one destination cycle, or "simplifying" a
handshake all pass every check the loop runs and are still wrong silicon.

Clock-generation logic has the same property for a different reason. A divider
is not on any critical path, its post-synthesis instance name is what the SDC
hangs a generated clock on, and rewriting it silently changes what every
downstream timing number was measured against.

So this is handled the way pipelining is -- structurally, before measurement,
rather than argued about afterwards. A design declares what is off limits:

    "protected": ["cdc_*", "clk_*_div*"]

and a candidate that edited any of it is rejected without being synthesised.

The rule has two halves, and both are needed:

  1. A line that ASSIGNS a protected signal may not change. That is the
     synchroniser and divider logic itself.
  2. Every REFERENCE to a protected signal must survive unchanged -- same
     identifier, same bit select, same number of them. That catches reading
     `cdc_x[0]` where the gold read `cdc_x[1]`, which bypasses a synchroniser
     stage without touching its assignment.

Reading a protected signal on a line that also does ordinary work is allowed,
because forbidding it forbids the optimisation. Measured: on netproc all three
specialists were rejected for rewriting

    xfrm_par <= par_chain[XW] ^ cdc_xfrm_from_look[1];

into the balanced-tree form `par_result ^ cdc_xfrm_from_look[1]` -- the exact
fix the design asks for, with the CDC reference untouched. A whole-line rule
blocks that, and blocking the fix is not a safe default. It is a broken one.

Nothing here parses Verilog. It is line-oriented text comparison, so it costs
nothing and cannot itself be wrong about the language.
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any, Iterable

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_LINE_COMMENT = re.compile(r"//.*$")


def _identifiers(line: str) -> list[str]:
    return _IDENT.findall(_LINE_COMMENT.sub("", line))


def _normalise(line: str) -> str:
    """Code content of a line, insensitive to comments and whitespace.

    A candidate that re-indented a synchroniser or edited a comment beside it
    has not changed the logic, and failing it would train the loop to avoid
    touching the file at all.
    """
    return " ".join(_LINE_COMMENT.sub("", line).split())


def matches(name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def protected_lines(text: str, patterns: Iterable[str]) -> list[tuple[int, str]]:
    """(1-based line number, normalised content) for every line that ASSIGNS a
    protected signal -- the synchroniser and divider bodies themselves."""
    pats = list(patterns)
    if not pats:
        return []
    out: list[tuple[int, str]] = []
    for n, line in enumerate(text.splitlines(), start=1):
        code = _normalise(line)
        if code and _assigns_protected(line, pats):
            out.append((n, code))
    return out


def _assigns_protected(line: str, patterns: Iterable[str]) -> bool:
    """Does this line drive a protected signal?

    Matches `cdc_x <= ...`, `if (!rst_n) cdc_x <= 0;` and `assign clk_d = ...`
    alike. A comparison such as `if (cdc_x <= 3)` is misread as an assignment,
    which only makes the rule stricter on that line -- safe in the direction
    that matters.
    """
    for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_$]*)\s*(?:\[[^\]]*\])?\s*(<=|=)(?!=)",
                         _LINE_COMMENT.sub("", line)):
        if matches(m.group(1), patterns):
            return True
    return False


def references(text: str, patterns: Iterable[str]) -> list[str]:
    """Every mention of a protected signal, in order, with its bit select.

    The select is part of the reference on purpose: `cdc_x[1]` and `cdc_x[0]`
    are different signals, and swapping one for the other is how a synchroniser
    stage gets bypassed without its assignment being touched.
    """
    pats = list(patterns)
    if not pats:
        return []
    out: list[str] = []
    for line in text.splitlines():
        for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_$]*)(\s*\[[^\]]*\])?",
                             _LINE_COMMENT.sub("", line)):
            if matches(m.group(1), pats):
                sel = (m.group(2) or "").replace(" ", "")
                out.append(m.group(1) + sel)
    return out


def regions(text: str, patterns: Iterable[str]) -> list[tuple[int, int]]:
    """Protected assignment lines merged into contiguous 1-based ranges.

    Used to keep protected logic out of a target's editable scope, so an agent
    is not handed something it will then be rejected for changing.
    """
    nums = [n for n, _ in protected_lines(text, patterns)]
    out: list[tuple[int, int]] = []
    for n in nums:
        if out and n == out[-1][1] + 1:
            out[-1] = (out[-1][0], n)
        else:
            out.append((n, n))
    return out


def violations(parent: str, candidate: str,
               patterns: Iterable[str]) -> list[str]:
    """What the candidate did to protected logic. Empty means it left it alone."""
    pats = list(patterns)
    if not pats:
        return []
    out: list[str] = []

    before = [c for _, c in protected_lines(parent, pats)]
    after = [c for _, c in protected_lines(candidate, pats)]
    if before != after:
        lost = [c for c in before if c not in after]
        gained = [c for c in after if c not in before]
        if len(after) < len(before):
            out.append(f"{len(before) - len(after)} protected assignment(s) removed")
        elif len(after) > len(before):
            out.append(f"{len(after) - len(before)} protected assignment(s) added")
        for c in lost[:3]:
            out.append(f"removed or altered: {c[:100]}")
        for c in gained[:3]:
            out.append(f"introduced: {c[:100]}")
        if not out:
            out.append("protected assignments reordered")

    rb, ra = references(parent, pats), references(candidate, pats)
    if rb != ra:
        from collections import Counter
        cb, ca = Counter(rb), Counter(ra)
        for name in sorted(set(cb) | set(ca)):
            if cb[name] != ca[name]:
                out.append(f"reference to {name} changed: "
                           f"{cb[name]} -> {ca[name]} occurrence(s)")
    return out


def check_files(parent: dict[str, str], candidate: dict[str, str],
                patterns: Iterable[str]) -> dict[str, Any]:
    """Run the check across a whole design, keyed by file name.

    A file the candidate dropped entirely counts as removing everything
    protected in it, which is otherwise an easy way through a per-file check.
    """
    pats = list(patterns)
    if not pats:
        return {"ok": True, "patterns": [], "files": {}}
    bad: dict[str, list[str]] = {}
    for name, text in parent.items():
        cand = candidate.get(name)
        if cand is None:
            if protected_lines(text, pats):
                bad[name] = ["file removed, and it held protected logic"]
            continue
        v = violations(text, cand, pats)
        if v:
            bad[name] = v
    return {"ok": not bad, "patterns": pats, "files": bad}


def describe(patterns: Iterable[str]) -> str:
    """What an agent is told. Says why, because a rule whose reason is hidden
    gets argued with."""
    pats = list(patterns)
    if not pats:
        return "<nothing declared off limits for this design>"
    return (
        "Any identifier matching: " + ", ".join(f"`{p}`" for p in pats) + "\n\n"
        "These are clock-domain crossings and clock generation. The equivalence\n"
        "check CUTS these boundaries, so it cannot tell you that a change here\n"
        "is wrong -- a rewritten synchroniser passes every check this loop runs\n"
        "and is still broken silicon. None of it is on a critical path, so\n"
        "there is nothing to gain by touching it.\n\n"
        "A candidate that adds, removes or alters any line mentioning one of\n"
        "these is rejected mechanically, before synthesis. Leave them exactly\n"
        "as they are, including when moving surrounding code.")


def render(result: dict[str, Any]) -> str:
    if result.get("ok"):
        return "protected regions untouched"
    lines = [f"protected regions edited (patterns: "
             f"{', '.join(result.get('patterns') or [])})"]
    for name, why in (result.get("files") or {}).items():
        lines.append(f"  {name}:")
        lines += [f"    - {w}" for w in why]
    return "\n".join(lines)
