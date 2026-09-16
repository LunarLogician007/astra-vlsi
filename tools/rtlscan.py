#!/usr/bin/env python3
"""Pre-synthesis RTL scan: which construct to look at before any tool runs.

The optimisation loop is grounded in measurement -- every claim it acts on
comes from a timing report. This module runs *before* there is a report, which
makes it the weakest evidence in the pipeline, and it is built to admit that.

It is a smell detector, not a delay model. It finds structures that are known
to synthesise into deep logic -- a serial reduction chain where a tree would
do, a wide operator, a comparison that cannot change value, a long ternary
cascade -- and reports each one with the number that produced it, so the
finding can be checked rather than believed. The depth figures are *ordinal*:
useful for saying "this expression is deeper than that one", meaningless as
nanoseconds.

Lexical, not a parser. It strips comments, reads declarations and assignments
with regular expressions, and gives up gracefully on anything it cannot make
sense of. Verilog is not a regular language and this does not pretend
otherwise; the cost of being wrong is a wasted suggestion that the equivalence
check and the timing report will both refuse to ratify.

Findings use the same {pattern, evidence, metric} shape as rtl_map.diagnose,
so they flow into the same prompt machinery as everything measured.
"""

from __future__ import annotations

import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Thresholds. Each is the point past which the construct reliably costs a
# level of logic that a restructuring can recover -- not a hard rule.
WIDE_OPERATOR = 16      # bits, above which an add/compare has a real carry chain
WIDE_MULTIPLY = 8       # bits, above which a multiply is an array not a few gates
CHAIN_LENGTH = 3        # associative ops in series before a tree is worth it
TERNARY_DEPTH = 4       # cascaded ?: before it is a priority encoder
DEEP_EXPRESSION = 12    # estimated levels in one continuous assignment

_KEYWORDS = {
    "module", "endmodule", "begin", "end", "if", "else", "case", "endcase",
    "always", "assign", "wire", "reg", "logic", "input", "output", "inout",
    "parameter", "localparam", "posedge", "negedge", "signed", "unsigned",
    "default", "generate", "endgenerate", "for", "while", "function",
    "endfunction", "integer", "genvar", "initial", "casez", "casex",
}


# --------------------------------------------------------------------------
# lexing
# --------------------------------------------------------------------------


def strip_comments(text: str) -> str:
    """Blank out comments, keeping line numbers intact.

    Line numbers are the whole point of a finding, so comments are replaced
    with spaces rather than removed. String literals are respected so that a
    `//` inside one does not eat the rest of the line.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:min(j + 1, n)])
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join(c if c == "\n" else " " for c in text[i:j]))
            i = j
        else:
            out.append(ch)
            i += 1
    return "".join(out)


@dataclass(frozen=True)
class Decl:
    name: str
    width: int
    signed: bool
    kind: str            # wire | reg | input | output | localparam | parameter
    file: str
    line: int
    value: str = ""      # localparam/parameter only


_DECL = re.compile(
    r"^\s*(?P<kind>input|output|inout|wire|reg|logic|localparam|parameter)\b"
    r"(?P<mods>(?:\s+(?:wire|reg|logic|signed|unsigned|integer))*)"
    r"(?:\s*\[\s*(?P<msb>[^\]:]+?)\s*:\s*(?P<lsb>[^\]]+?)\s*\])?"
    r"\s+(?P<rest>.+)$")

_ASSIGN = re.compile(
    r"(?P<lhs>[A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*(?P<op><=|=)(?!=)(?P<rhs>[^;]*);")


def _int(expr: str, params: dict[str, int]) -> int | None:
    """Evaluate a width expression made of integers and known parameters."""
    e = (expr or "").strip()
    if not e:
        return None
    for name, val in params.items():
        e = re.sub(rf"(?<![\w$]){re.escape(name)}(?![\w$])", str(val), e)
    if not re.fullmatch(r"[\d\s+\-*/()]+", e):
        return None
    try:
        return int(eval(e, {"__builtins__": {}}, {}))   # noqa: S307 -- digits only
    except (SyntaxError, ValueError, ZeroDivisionError, TypeError):
        return None


def _literal(text: str) -> int | None:
    """Value of a Verilog integer literal, sized or plain."""
    t = (text or "").strip().replace("_", "")
    m = re.fullmatch(r"(?:\d+)?'[sS]?([bodhBODH])([0-9a-fA-F]+)", t)
    if m:
        base = {"b": 2, "o": 8, "d": 10, "h": 16}[m.group(1).lower()]
        try:
            return int(m.group(2), base)
        except ValueError:
            return None
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    return None


def declarations(text: str, fname: str) -> tuple[dict[str, Decl], dict[str, int]]:
    """Every declared name, and the subset that resolves to a constant."""
    decls: dict[str, Decl] = {}
    params: dict[str, int] = {}
    for i, raw in enumerate(text.splitlines(), start=1):
        m = _DECL.match(raw)
        if not m:
            continue
        kind, mods, rest = m.group("kind"), m.group("mods") or "", m.group("rest")
        signed = "signed" in mods or "signed" in (raw[:m.start("rest")])
        width = 1
        if m.group("msb") is not None:
            msb, lsb = _int(m.group("msb"), params), _int(m.group("lsb"), params)
            width = abs(msb - lsb) + 1 if msb is not None and lsb is not None else 0

        if kind in ("localparam", "parameter"):
            for part in _split_commas(rest.rstrip(";,")):
                if "=" not in part:
                    continue
                name, _, val = part.partition("=")
                name, val = name.strip(), val.strip()
                if not name.isidentifier():
                    continue
                decls[name] = Decl(name, width, signed, kind, fname, i, val)
                lit = _literal(val)
                if lit is None:
                    lit = _int(val, params)
                if lit is not None:
                    params[name] = lit
            continue

        for part in _split_commas(rest.rstrip(";,")):
            name = part.split("=")[0].strip().split("[")[0].strip()
            if name.isidentifier() and name not in _KEYWORDS:
                decls.setdefault(name, Decl(name, width, signed, kind, fname, i))
    return decls, params


def _split_commas(text: str) -> list[str]:
    """Split on commas outside brackets and braces."""
    out, depth, buf = [], 0, []
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth <= 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return [p for p in (s.strip() for s in out) if p]


@dataclass
class Assign:
    lhs: str
    rhs: str
    file: str
    line: int
    blocking: bool
    width: int = 0
    refs: list[str] = field(default_factory=list)
    masked: str = ""        # rhs with index/width expressions blanked
    constant: bool = False  # a localparam/parameter value, not logic
    end_line: int = 0       # statements wrap; this is where it ends


def statements(text: str) -> list[tuple[str, int, int]]:
    """`;`-terminated statements, each with the line span it occupies.

    Scanning line by line misses every expression that wraps, and real RTL
    wraps constantly -- a nested ternary, a generate-loop body, any operand
    list wider than eighty columns. On the designs here that gap hid a
    saturation select and an entire forty-deep priority cascade, which is to
    say it hid the thing the scan exists to find.
    """
    out: list[tuple[str, int, int]] = []
    buf: list[str] = []
    line = start = 1
    for ch in text:
        if ch == ";":
            stmt = "".join(buf)
            if stmt.strip():
                out.append((stmt + ";", start, line))
            buf, start = [], line
            continue
        if not buf and not ch.isspace():
            start = line
        buf.append(ch)
        if ch == "\n":
            line += 1
    return out


_FOR_HDR = re.compile(r"\bfor\s*\((?:[^()]|\([^()]*\))*\)")


def blank_for_headers(text: str) -> str:
    """Blank `for (...)` headers, keeping the line count.

    A loop header holds the only semicolons Verilog allows inside parentheses,
    so splitting statements on `;` cuts one into three -- and the last fragment
    (`i = i - 1) begin : prio  assign pri[...] = ...`) swallows the assignment
    that follows it. That is how an entire forty-deep priority cascade managed
    to be invisible to this scan: it parsed as an assignment to the loop
    variable.
    """
    return _FOR_HDR.sub(
        lambda m: "".join(c if c == "\n" else " " for c in m.group(0)), text)


def assignments(text: str, fname: str, decls: dict[str, Decl]) -> list[Assign]:
    """Continuous and procedural assignments, with the names each one reads."""
    out: list[Assign] = []
    for raw, i, end in statements(blank_for_headers(text)):
        for m in _ASSIGN.finditer(raw):
            lhs, rhs = m.group("lhs"), (m.group("rhs") or "").strip()
            if not rhs or lhs in _KEYWORDS:
                continue
            masked = mask_index_context(rhs)
            refs = [t for t in re.findall(r"(?<![\w$'])[A-Za-z_]\w*", masked)
                    if t not in _KEYWORDS]
            out.append(Assign(lhs=lhs, rhs=rhs, file=fname, line=i, end_line=end,
                              blocking=m.group("op") == "=",
                              width=decls[lhs].width if lhs in decls else 0,
                              refs=refs, masked=masked,
                              constant=(lhs in decls
                                        and decls[lhs].kind in ("localparam",
                                                                "parameter"))))
    return out


# --------------------------------------------------------------------------
# depth model  (ordinal, not physical -- see the module docstring)
# --------------------------------------------------------------------------


def _levels(op: str, width: int) -> int:
    w = max(1, width)
    if op in ("+", "-"):
        return int(math.ceil(math.log2(w))) + 1
    if op == "*":
        return 2 * int(math.ceil(math.log2(w))) + 1
    if op in (">", "<", ">=", "<=", "==", "!="):
        return int(math.ceil(math.log2(w))) + 1
    return 1                           # bitwise, logical, reduction


def _shift_levels(rhs: str, op: str, width: int) -> int:
    """A shift by a constant is wiring; a shift by a signal is a barrel mux."""
    m = re.search(re.escape(op) + r"\s*(\S+)", rhs or "")
    amount = m.group(1) if m else ""
    if _literal(amount) is not None:
        return 0
    return int(math.ceil(math.log2(max(2, width))))


_OPS = re.compile(r"(<<<|>>>|<<|>>|<=|>=|==|!=|[+\-*/&|^~?<>])")

_BITSEL = re.compile(r"\[[^\[\]]*\]")
_REPCOUNT = re.compile(r"\((?:[^()]|\([^()]*\))*\)(?=\s*\{)")


def mask_index_context(rhs: str) -> str:
    """Blank out width and index expressions, keeping the string's length.

    `{{(ACC-2*W){p0[2*W-1]}}, p0}` contains a `*` and two `-`, and none of them
    are datapath arithmetic -- they size a sign extension. Counting them made
    the reference design report multiplies that do not exist and, worse, made
    the reachability bound follow a multiply branch it should never have taken.
    Index brackets and replication counts are therefore blanked before any
    operator is read out of an expression.
    """
    prev = None
    out = rhs or ""
    while prev != out:                     # nested selects: [a[b]] needs two passes
        prev = out
        out = _BITSEL.sub(lambda m: " " * len(m.group(0)), out)
    prev = None
    while prev != out:
        prev = out
        out = _REPCOUNT.sub(lambda m: " " * len(m.group(0)), out)
    return out


def expr_depth(rhs: str, width: int) -> tuple[int, list[str]]:
    """Ordinal depth of one right-hand side, and the operators that built it.

    Sums rather than maxes: a chain of operators in one expression really does
    stack, and the point of the number is to order expressions against each
    other, not to predict a delay.
    """
    masked = mask_index_context(rhs)
    ops = [o for o in _OPS.findall(masked) if o != "~"]
    if not ops:
        return 0, []
    ternaries = masked.count("?")
    depth = ternaries
    for o in ops:
        if o == "?":
            continue
        depth += (_shift_levels(masked, o, width)
                  if o in ("<<", ">>", "<<<", ">>>") else _levels(o, width))
    return depth, ops


# --------------------------------------------------------------------------
# findings
# --------------------------------------------------------------------------


def _finding(pattern: str, evidence: str, metric: dict[str, Any],
             file: str, line: int, end_line: int | None = None,
             severity: str = "medium") -> dict[str, Any]:
    return {"pattern": pattern, "evidence": evidence, "metric": metric,
            "file": file, "line": line, "end_line": end_line or line,
            "severity": severity}


def serial_chains(assigns: list[Assign], decls: dict[str, Decl]) -> list[dict]:
    """Reductions written as a chain where a tree is the same function.

    ``s1 = s0 + p1; s2 = s1 + p2; s3 = s2 + p3`` is depth 3 in adders. The same
    four terms as a balanced tree is depth 2, and for eight terms it is 3
    instead of 7 -- the gap widens with every tap.
    """
    live = [a for a in assigns if not a.constant]
    by_lhs = {a.lhs: a for a in live}
    out: list[dict] = []
    consumed: set[str] = set()

    for a in live:
        if a.lhs in consumed:
            continue
        for op in ("+", "|", "&", "^", "*"):
            if op not in a.masked:
                continue
            chain, cur = [a], a
            while True:
                prev = next((r for r in cur.refs
                             if r in by_lhs and op in by_lhs[r].masked
                             and by_lhs[r].lhs != cur.lhs), None)
                if prev is None or by_lhs[prev] in chain:
                    break
                cur = by_lhs[prev]
                chain.append(cur)
            if len(chain) >= CHAIN_LENGTH:
                chain.sort(key=lambda x: x.line)
                width = max((x.width for x in chain), default=0)
                tree = int(math.ceil(math.log2(len(chain) + 1)))
                out.append(_finding(
                    "serial reduction chain written as a chain, not a tree",
                    f"{len(chain)} '{op}' operations in series at width {width} "
                    f"({', '.join(x.lhs for x in chain)}) -- depth {len(chain)} "
                    f"where a balanced tree of the same terms is depth {tree}",
                    {"length": len(chain), "op": op, "width": width,
                     "tree_depth": tree, "signals": [x.lhs for x in chain]},
                    chain[0].file, chain[0].line, chain[-1].line,
                    severity="high"))
                consumed.update(x.lhs for x in chain)
                break
    return out


_FOR = re.compile(r"\bfor\s*\(\s*\w+\s*=\s*(?P<init>[^;]+);"
                  r"\s*\w+\s*(?P<cmp>[<>]=?)\s*(?P<bound>[^;]+);")


def generate_chains(text: str, fname: str, assigns: list[Assign],
                    decls: dict[str, Decl], params: dict[str, int]) -> list[dict]:
    """Serial chains built structurally by a generate loop.

    A lexical scan sees one assignment where elaboration produces forty. The
    tell is self-reference: `pri[8*i] = sel ? d : pri[8*(i+1)]` names its own
    net on the right at a different index, which is exactly how a ripple carry,
    a priority cascade and a shift chain are all written. The loop bound gives
    the depth, so the finding can carry a real number rather than a shrug.

    Without this the scan reports nothing at all on a priority encoder, which
    is the single most common deep-control structure there is.
    """
    bounds: list[tuple[int, int, int]] = []       # (start_line, end_line, trips)
    lines = text.splitlines()
    for i, raw in enumerate(lines, start=1):
        m = _FOR.search(raw)
        if not m:
            continue
        lo = _int(m.group("init"), params)
        hi = _int(m.group("bound"), params)
        if lo is None or hi is None:
            continue
        trips = abs(hi - lo) + (1 if "=" in m.group("cmp") else 0)
        if trips > 1:
            bounds.append((i, i + 200, trips))

    out: list[dict] = []
    for a in assigns:
        if a.constant or a.lhs not in a.refs or a.lhs not in decls:
            continue
        trips = next((t for lo, hi, t in bounds
                      if lo <= a.line <= hi or lo <= a.end_line <= hi), None)
        if trips is None or trips < CHAIN_LENGTH:
            continue
        width = decls[a.lhs].width if a.lhs in decls else 0
        tree = int(math.ceil(math.log2(trips)))
        out.append(_finding(
            "serial chain elaborated by a generate loop",
            f"`{a.lhs}` is defined in terms of itself across {trips} loop "
            f"iterations -- {trips} levels in series, where a balanced tree "
            f"over the same terms is {tree}. The lexical source shows one "
            f"assignment; synthesis produces {trips}",
            {"trips": trips, "tree_depth": tree, "signal": a.lhs,
             "width": width},
            a.file, a.line, a.end_line, severity="high"))
    return out


def high_fanout_broadcast(assigns: list[Assign], decls: dict[str, Decl],
                          params: dict[str, int]) -> list[dict]:
    """A single registered vector read by every stage of a generate chain.

    Cheap to spot and worth spotting: the driver of such a net carries the
    whole chain's load, and that load sits on the launching path.
    """
    out: list[dict] = []
    counts: dict[str, int] = {}
    for a in assigns:
        for r in set(a.refs):
            if r in decls and decls[r].kind in ("wire", "reg"):
                counts[r] = counts.get(r, 0) + 1
    for a in assigns:
        if a.constant or a.lhs not in a.refs or a.lhs not in decls:
            continue
        for r in sorted(set(a.refs)):
            if r == a.lhs or r not in decls:
                continue
            w = decls[r].width
            if w >= 8 and decls[r].kind == "reg":
                out.append(_finding(
                    "one register broadcast to every stage of a chain",
                    f"`{r}` ({w} bits) is read inside the loop that builds "
                    f"`{a.lhs}`, so every elaborated stage loads it at once",
                    {"signal": r, "width": w, "chain": a.lhs},
                    a.file, a.line, severity="medium"))
                break
    return out


def wide_operators(assigns: list[Assign], decls: dict[str, Decl]) -> list[dict]:
    """Arithmetic wide enough to carry a real carry chain."""
    out: list[dict] = []
    for a in assigns:
        if a.constant:
            continue
        w = a.width or max((decls[r].width for r in a.refs if r in decls),
                           default=0)
        mults = a.masked.count("*")
        if mults:
            # The operands set the array size, not the (wider) result. Taking
            # the result width made a 16x16 multiply assigned to a 40-bit wire
            # report as a 40x40 array.
            operands = [decls[r].width for r in a.refs
                        if r in decls and decls[r].kind not in ("localparam",
                                                                "parameter")]
            mw = min(operands) if operands else w
            if mw >= WIDE_MULTIPLY:
                out.append(_finding(
                    "unpipelined multiply in one cycle",
                    f"{mults} multiply(s) at {mw} bits on `{a.lhs}` -- a "
                    f"{mw}x{mw} array is roughly {2 * int(math.ceil(math.log2(max(mw, 2))))} "
                    f"levels before anything downstream starts",
                    {"multiplies": mults, "operand_width": mw},
                    a.file, a.line, severity="high"))
        adds = len(re.findall(r"[+\-](?![\-+])", a.masked))
        if adds and w >= WIDE_OPERATOR:
            out.append(_finding(
                "wide arithmetic in one expression",
                f"{adds} add/subtract at {w} bits on `{a.lhs}` -- "
                f"{_levels('+', w)} levels of carry each",
                {"ops": adds, "width": w, "levels_each": _levels("+", w)},
                a.file, a.line))
    return out


def dead_comparisons(assigns: list[Assign], decls: dict[str, Decl],
                     params: dict[str, int]) -> list[dict]:
    """Comparisons whose result cannot vary, or barely can.

    The reference design has one: a 40-bit signed accumulator compared against
    2^37-1 when the four products it sums cannot exceed 2^32 in magnitude. The
    comparison is real logic sitting at the end of the critical path deciding
    something that is already decided.
    """
    out: list[dict] = []
    for a in assigns:
        if a.constant:
            continue
        for m in re.finditer(r"(?P<lhs>[A-Za-z_]\w*)\s*(?P<op>>=|<=|>|<)\s*"
                             r"(?P<rhs>-?\s*[A-Za-z_]\w*|-?\s*[\d']\S*)", a.masked):
            name, op = m.group("lhs"), m.group("op")
            rhs = m.group("rhs").replace(" ", "")
            neg = rhs.startswith("-")
            bound = params.get(rhs.lstrip("-"))
            if bound is None:
                bound = _literal(rhs.lstrip("-"))
            if bound is None or name not in decls:
                continue

            reach = _reachable(name, assigns, decls)
            if reach is None:
                continue
            limit = -bound if neg else bound
            if (op in (">", ">=") and reach <= abs(limit)) or \
               (op in ("<", "<=") and neg and reach <= abs(limit)):
                out.append(_finding(
                    "comparison against an unreachable bound",
                    f"`{name} {op} {rhs}` compares a value whose magnitude "
                    f"cannot exceed 2^{int(math.log2(reach)) if reach > 0 else 0} "
                    f"against {limit} -- {abs(limit) / reach:.0f}x of headroom, so "
                    f"the comparison's result is fixed",
                    {"signal": name, "op": op, "bound": limit,
                     "reachable_magnitude": reach},
                    a.file, a.line, severity="high"))
    return out


def _reachable(name: str, assigns: list[Assign],
               decls: dict[str, Decl], depth: int = 6) -> float | None:
    """Crude bound on |name|, propagated back through its drivers.

    Deliberately conservative and deliberately shallow: it only follows adds
    and multiplies of declared widths, which is enough for the accumulator
    case and returns None the moment it meets anything else.
    """
    if depth <= 0 or name not in decls:
        return None
    drv = next((a for a in assigns if a.lhs == name and not a.constant), None)
    if drv is None:
        d = decls[name]
        return 2.0 ** max(0, d.width - (1 if d.signed else 0)) if d.width else None

    terms = [r for r in drv.refs if r in decls]
    if not terms:
        return None
    if "*" in drv.masked:
        widths = [decls[t].width - (1 if decls[t].signed else 0) for t in terms
                  if decls[t].kind not in ("localparam", "parameter")]
        return 2.0 ** sum(sorted(widths, reverse=True)[:2]) if widths else None
    bounds = [_reachable(t, assigns, decls, depth - 1) for t in terms]
    if any(b is None for b in bounds):
        return None
    return float(sum(bounds))       # a sum of terms is bounded by the sum


def ternary_chains(assigns: list[Assign]) -> list[dict]:
    """Cascaded ?: -- a priority encoder written as a nest."""
    out: list[dict] = []
    for a in assigns:
        if a.constant:
            continue
        n = a.masked.count("?")
        if n >= TERNARY_DEPTH:
            out.append(_finding(
                "cascaded conditional select",
                f"{n} nested `?:` on `{a.lhs}` -- a priority chain {n} muxes "
                f"deep, where a one-hot select is one mux",
                {"depth": n}, a.file, a.line))
    return out


def deep_expressions(assigns: list[Assign], decls: dict[str, Decl]) -> list[dict]:
    out: list[dict] = []
    for a in assigns:
        if a.constant:
            continue
        w = a.width or max((decls[r].width for r in a.refs if r in decls),
                           default=1)
        depth, ops = expr_depth(a.rhs, w)
        if depth >= DEEP_EXPRESSION:
            out.append(_finding(
                "deep single-cycle expression",
                f"`{a.lhs}` estimates {depth} levels in one assignment "
                f"(operators: {' '.join(sorted(set(ops)))})",
                {"depth": depth, "width": w, "ops": sorted(set(ops))},
                a.file, a.line))
    return out


def sign_extension(assigns: list[Assign]) -> list[dict]:
    """Hand-written sign extension repeated across a datapath.

    Not a timing problem in itself -- it is a signal that the widths were
    widened by hand, which is where accidental extra arithmetic tends to hide.
    """
    hits = [a for a in assigns if not a.constant
            and re.search(r"\{\s*\{\s*\(?\s*[\w\s\-*+]+\)?\s*\{", a.rhs)]
    if len(hits) < 2:
        return []
    return [_finding(
        "hand-written sign extension repeated across the datapath",
        f"{len(hits)} replicated-bit extensions "
        f"({', '.join(a.lhs for a in hits[:6])}) -- widening by hand before "
        f"each operation, rather than once at the boundary",
        {"count": len(hits), "signals": [a.lhs for a in hits]},
        hits[0].file, hits[0].line, hits[-1].line, severity="low")]


# --------------------------------------------------------------------------
# top level
# --------------------------------------------------------------------------


def scan(files: list[Path]) -> dict[str, Any]:
    """Every finding across a design's RTL, worst first."""
    decls: dict[str, Decl] = {}
    params: dict[str, int] = {}
    assigns: list[Assign] = []
    scanned: list[str] = []
    sources: list[tuple[str, str]] = []

    for f in files:
        try:
            text = strip_comments(Path(f).read_text())
        except OSError:
            continue
        scanned.append(Path(f).name)
        sources.append((Path(f).name, text))
        d, p = declarations(text, Path(f).name)
        decls.update(d)
        params.update(p)
        assigns.extend(assignments(text, Path(f).name, d))

    gen: list[dict] = []
    for fname, text in sources:
        mine = [a for a in assigns if a.file == fname]
        gen += generate_chains(text, fname, mine, decls, params)
        if gen:
            gen += high_fanout_broadcast(mine, decls, params)

    findings = (gen
                + serial_chains(assigns, decls)
                + wide_operators(assigns, decls)
                + dead_comparisons(assigns, decls, params)
                + ternary_chains(assigns)
                + deep_expressions(assigns, decls)
                + sign_extension(assigns))
    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 3), f["line"]))

    return {
        "files": scanned,
        "findings": findings,
        "counts": {"declarations": len(decls), "assignments": len(assigns),
                   "parameters": len(params)},
    }


def render(report: dict[str, Any]) -> str:
    L = [f"pre-synthesis scan of {', '.join(report['files']) or '(nothing)'}",
         f"{report['counts']['assignments']} assignment(s), "
         f"{report['counts']['declarations']} declaration(s)", ""]
    if not report["findings"]:
        L.append("no structural smells found -- nothing here is obviously "
                 "costing logic levels before synthesis has run.")
        return "\n".join(L)
    L.append("findings, worst first (each is a hypothesis with its evidence, "
             "not a measurement):")
    L.append("")
    for f in report["findings"]:
        loc = f"{f['file']}:{f['line']}"
        if f["end_line"] != f["line"]:
            loc += f"-{f['end_line']}"
        L.append(f"  [{f['severity']:<6}] {f['pattern']}")
        L.append(f"    at {loc}")
        L.append(f"    {f['evidence']}")
        L.append("")
    return "\n".join(L)


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="astra-scan",
        description="Structural smells in RTL, before any tool has run.")
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv[1:])
    report = scan(args.files)
    print(json.dumps(report, indent=2, default=str) if args.json
          else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
