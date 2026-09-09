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

The rule is deliberately blunt: a line mentioning a protected identifier may
not be added, removed, or changed. Blunt is right here. A subtle rule invites
a candidate to argue it stayed within the spirit of one, and the whole point is
that no tool downstream can check whether it did.

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
    """(1-based line number, normalised content) for every protected line."""
    pats = list(patterns)
    if not pats:
        return []
    out: list[tuple[int, str]] = []
    for n, line in enumerate(text.splitlines(), start=1):
        code = _normalise(line)
        if code and any(matches(i, pats) for i in _identifiers(line)):
            out.append((n, code))
    return out


def regions(text: str, patterns: Iterable[str]) -> list[tuple[int, int]]:
    """Protected lines merged into contiguous 1-based inclusive ranges.

    Used to keep protected lines out of a target's editable scope, so an agent
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
    """What the candidate did to protected logic. Empty means it left it alone.

    Compares the ordered sequence of protected lines rather than diffing the
    whole file, so moving unrelated code around is free and touching a
    synchroniser is not.
    """
    pats = list(patterns)
    if not pats:
        return []
    before = [c for _, c in protected_lines(parent, pats)]
    after = [c for _, c in protected_lines(candidate, pats)]
    if before == after:
        return []

    out: list[str] = []
    lost = [c for c in before if c not in after]
    gained = [c for c in after if c not in before]
    if len(after) < len(before):
        out.append(f"{len(before) - len(after)} protected line(s) removed")
    elif len(after) > len(before):
        out.append(f"{len(after) - len(before)} protected line(s) added")
    for c in lost[:4]:
        out.append(f"removed or altered: {c[:100]}")
    for c in gained[:4]:
        out.append(f"introduced: {c[:100]}")
    if not out:
        out.append("protected lines reordered")
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
