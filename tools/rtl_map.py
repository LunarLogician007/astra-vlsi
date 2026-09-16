#!/usr/bin/env python3
"""Critical-path -> RTL localisation.

The paper's Timing Analysis Agent "performs path-to-RTL mapping by locating
the startpoint and endpoint registers and tracing the intermediate
combinational logic back to the corresponding RTL regions", then "diagnoses
likely root causes of delay". Everything here is the mechanical half of that:
it turns an OpenSTA path into named RTL line spans plus grounded structural
facts, so the LLM half reasons over evidence instead of guessing from cell
names.

Two sources of truth, tried in order:

1.  ``src`` attributes in Yosys's ``netlist.json``. Authoritative -- Yosys
    carries them from the RTL through synthesis. They survive on everything
    with a *public* name (registers, named wires, ports), which is exactly
    what write_verilog also emits verbatim, so netlist and JSON agree on
    those names with no mangling in between.

2.  Identifier lookup in the RTL text. Covers the rest: OpenSTA reports
    abc-generated cells as ``_14637_`` because write_verilog renames every
    ``$``-prefixed internal name to a running counter, and those cells have no
    meaningful src anyway. What they *are* attached to are named nets, and
    those resolve.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any

# "mac_chain.v:66.29-66.36" and multi-span "a.v:1.1-1.2|a.v:9.1-9.4"
_SRC = re.compile(r"^(?P<file>.+?):(?P<l0>\d+)(?:\.(?P<c0>\d+))?"
                  r"(?:-(?P<l1>\d+)(?:\.(?P<c1>\d+))?)?$")
_BITSEL = re.compile(r"\[\d+(?::\d+)?\]")
_MANGLED = re.compile(r"^_\d+_$")
# Yosys `autoname` names an anonymous cell after the path that reaches it:
#   <origin-net>[bit]_<CELL>_<PIN>_<CELL>_<PIN>...
# e.g. `acc_out[33]_DFF_X1_Q_D_AOI21_X1_ZN`. The leading segment, before the
# first upper-case cell-type token, is the RTL net the cone started from.
_AUTONAME = re.compile(r"^(?P<base>.+?)(?=_[A-Z][A-Z0-9]*(?:_|$))")


# ---------------------------------------------------------------------------
# netlist index
# ---------------------------------------------------------------------------


def parse_src(attr: str) -> list[dict[str, Any]]:
    """Yosys ``src`` attribute -> one or more {file, line, end_line} spans."""
    out: list[dict[str, Any]] = []
    for piece in str(attr).split("|"):
        m = _SRC.match(piece.strip())
        if not m:
            continue
        l0 = int(m.group("l0"))
        l1 = int(m.group("l1")) if m.group("l1") else l0
        out.append({"file": Path(m.group("file")).name,
                    "line": l0, "end_line": max(l0, l1)})
    return out


def load_netlist_index(path: Path) -> dict[str, Any]:
    """Public-name -> src map for every cell and net in the Yosys JSON.

    ``$``-prefixed names are skipped: they cannot be matched against what
    OpenSTA reports, so indexing them would only add noise.
    """
    idx: dict[str, Any] = {"cells": {}, "nets": {}, "modules": [], "available": False}
    if not path.is_file():
        return idx
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return idx

    for mod_name, mod in (data.get("modules") or {}).items():
        idx["modules"].append(mod_name.lstrip("\\"))
        for cname, cell in (mod.get("cells") or {}).items():
            if cname.startswith("$"):
                continue
            src = (cell.get("attributes") or {}).get("src")
            idx["cells"][cname.lstrip("\\")] = {
                "type": (cell.get("type") or "").lstrip("\\"),
                "src": parse_src(src) if src else [],
            }
        for nname, net in (mod.get("netnames") or {}).items():
            if nname.startswith("$"):
                continue
            src = (net.get("attributes") or {}).get("src")
            idx["nets"][nname.lstrip("\\")] = {
                "src": parse_src(src) if src else [],
                "width": len(net.get("bits") or []),
            }
    idx["available"] = bool(idx["cells"] or idx["nets"])
    return idx


# ---------------------------------------------------------------------------
# RTL index
# ---------------------------------------------------------------------------


class RtlIndex:
    """The design's Verilog, addressable by line and by identifier."""

    # An identifier's "home" is where it is declared or driven, in that order
    # of preference -- a continuous assignment tells you more about the logic
    # than the reg declaration does.
    _DECL = r"\b(?:wire|reg|logic|input|output|inout|localparam|parameter)\b"

    def __init__(self, files: list[Path]) -> None:
        self.files: OrderedDict[str, list[str]] = OrderedDict()
        for f in files:
            try:
                self.files[f.name] = f.read_text().splitlines()
            except OSError:
                continue

    def line(self, fname: str, n: int) -> str:
        lines = self.files.get(fname) or next(iter(self.files.values()), [])
        return lines[n - 1].rstrip() if 1 <= n <= len(lines) else ""

    def span(self, fname: str, a: int, b: int, cap: int = 12) -> list[str]:
        lines = self.files.get(fname) or next(iter(self.files.values()), [])
        a, b = max(1, a), min(len(lines), max(a, b))
        return [f"{i:5d} | {lines[i - 1].rstrip()}" for i in range(a, min(b, a + cap - 1) + 1)]

    def find_identifier(self, ident: str) -> list[dict[str, Any]]:
        """Where an RTL name is declared and where it is driven."""
        if not ident:
            return []
        word = re.compile(rf"(?<![\w$]){re.escape(ident)}(?![\w$])")
        hits: list[dict[str, Any]] = []
        for fname, lines in self.files.items():
            for i, text in enumerate(lines, start=1):
                stripped = text.strip()
                if stripped.startswith("//") or not word.search(text):
                    continue
                kind = None
                # Order matters: `wire [39:0] s1 = a + b;` is both a
                # declaration and a driver, and the assignment is the half
                # that describes the logic. Constants are the exception --
                # a localparam is a value, not a stage on a path.
                is_const = re.match(r"^\s*(?:localparam|parameter)\b", text)
                if not is_const and re.search(
                        rf"(?<![\w$]){re.escape(ident)}(?![\w$])"
                        rf"\s*(?:\[[^\]]*\]\s*)?(?:<=|=)(?!=)", text):
                    kind = "driver"
                elif re.search(
                        rf"{self._DECL}[^;=]*?(?<![\w$]){re.escape(ident)}(?![\w$])",
                        text):
                    # [^;=] rather than [^;]: once an `=` intervenes the
                    # identifier is on the initializer's right-hand side, so
                    # the line uses it, it does not declare it.
                    kind = "declaration"
                if kind:
                    hits.append({"file": fname, "line": i, "end_line": i,
                                 "kind": kind, "text": stripped})
        # Drivers first: they describe the logic, declarations only the type.
        # Within drivers, a constant assignment is almost always the reset
        # branch -- real for simulation, uninformative about the timing path.
        hits.sort(key=lambda h: (h["kind"] != "driver",
                                 self._is_constant_assign(h["text"]),
                                 h["line"]))
        return hits[:4]

    _RHS = re.compile(r"(?:<=|=)(?!=)(.*?);")
    _SIZED_LITERAL = re.compile(r"\d*'[sS]?[bodhBODH][0-9a-fA-FxXzZ_]+")
    _LOWER_IDENT = re.compile(r"(?<![\w$])[a-z_]\w*")

    @classmethod
    def _is_constant_assign(cls, text: str) -> bool:
        """True for `q <= 0;`, `q <= {W{1'b0}};` and the like.

        Nested replication braces defeat a balanced-brace regex, so the test
        is on what the right-hand side *names* instead: strip sized literals,
        then look for a lowercase identifier. Signals in this codebase are
        lower case and parameters are upper case, so an RHS with no lowercase
        name left in it is a constant -- which inside an always block means
        the reset branch.
        """
        m = cls._RHS.search(text)
        if not m:
            return False
        rhs = cls._SIZED_LITERAL.sub("", m.group(1))
        return not cls._LOWER_IDENT.search(rhs)


# ---------------------------------------------------------------------------
# name handling
# ---------------------------------------------------------------------------


def split_pin(pin: str) -> tuple[str, str | None]:
    """``a0_q[3]/Q`` -> ("a0_q[3]", "Q");  ``s2[9]`` -> ("s2[9]", None)."""
    if not pin:
        return "", None
    if "/" in pin:
        inst, _, port = pin.rpartition("/")
        return inst, port
    return pin, None


def base_ident(name: str) -> str:
    """Instance/net name stripped to the RTL identifier it came from.

    ``a0_q[3]`` -> ``a0_q``. Hierarchical paths keep only the leaf, since the
    flow flattens by default and the leaf is what appears in the RTL text.
    """
    name = _BITSEL.sub("", name or "").strip()
    if "." in name:
        name = name.rsplit(".", 1)[-1]
    return name


def is_mangled(name: str) -> bool:
    """True for write_verilog's ``_14637_`` renaming of an internal name."""
    return bool(_MANGLED.match(base_ident(name)))


def strip_autoname(name: str) -> str:
    """``acc_out[33]_DFF_X1_Q_D_AOI21_X1_ZN`` -> ``acc_out[33]``.

    Returns the name unchanged when it carries no cell-type chain, so callers
    can try the full name first and fall back to this without special-casing.
    """
    m = _AUTONAME.match(name or "")
    return m.group("base") if m else (name or "")


# ---------------------------------------------------------------------------
# mapping
# ---------------------------------------------------------------------------


def resolve(name: str, nl: dict[str, Any], rtl: RtlIndex) -> list[dict[str, Any]]:
    """RTL regions a netlist name came from. Empty for unresolvable names.

    The name is tried whole before its autoname prefix, so a real RTL signal
    that happens to contain an upper-case segment (``bus_A0``) resolves to
    itself rather than being truncated to ``bus``.
    """
    for candidate, tag in ((name, ""), (strip_autoname(name), " via cone origin")):
        ident = base_ident(candidate)
        if not ident or is_mangled(ident):
            continue

        for table in ("cells", "nets"):
            entry = (nl.get(table) or {}).get(ident)
            if entry and entry.get("src"):
                return [{**sp, "via": f"src attribute ({table[:-1]}){tag}"}
                        for sp in entry["src"]]

        hits = rtl.find_identifier(ident)
        if hits:
            return [{**h, "via": h.pop("kind") + tag} for h in hits]
    return []


def _region_key(r: dict[str, Any]) -> tuple[str, int, int]:
    return (r.get("file", ""), int(r.get("line", 0)), int(r.get("end_line", 0)))


def map_path(path: dict[str, Any], nl: dict[str, Any], rtl: RtlIndex) -> dict[str, Any]:
    """Localise one timing path onto RTL line spans.

    Regions come out in path order -- first stage that touched them first --
    because the order *is* the topology: it says which RTL expression feeds
    which, and that is what a rewrite has to restructure.
    """
    stages = path.get("stages") or []
    endpoints = {"startpoint": path.get("startpoint"), "endpoint": path.get("endpoint")}

    regions: OrderedDict[tuple, dict[str, Any]] = OrderedDict()
    resolved = unresolved = 0

    def add(name: str, role: str, delay: float | None, cell: str | None) -> bool:
        found = resolve(name, nl, rtl)
        if not found:
            return False
        # A single `src` attribute can legitimately span several places, and
        # all of them are real. Several *regex* hits are one ambiguity, not
        # several regions, so only the best-ranked one is credited -- crediting
        # all of them would multiply one stage's delay across every line that
        # happens to mention the signal.
        if not str(found[0].get("via", "")).startswith("src attribute"):
            found = found[:1]
        for r in found:
            key = _region_key(r)
            slot = regions.setdefault(key, {
                "file": r.get("file"), "line": r.get("line"),
                "end_line": r.get("end_line"), "via": r.get("via"),
                "stages": 0, "delay_ns": 0.0, "roles": [], "cells": Counter(),
                "signals": [],
            })
            # Start- and endpoint registration marks a region's role; it is
            # not an extra stage on the path and carries no delay of its own.
            if role == "path":
                slot["stages"] += 1
                slot["delay_ns"] += float(delay or 0.0)
            if role not in slot["roles"]:
                slot["roles"].append(role)
            if cell:
                slot["cells"][cell] += 1
            # The RTL name, not the autoname chain that reached it: a chain
            # can run to hundreds of characters, and dozens of them would
            # dominate the prompt while saying nothing the cell histogram
            # does not already say.
            ident = base_ident(strip_autoname(name))
            if ident and ident not in slot["signals"]:
                slot["signals"].append(ident)
        return True

    for role, name in endpoints.items():
        if name:
            add(name, role, None, None)

    for st in stages:
        inst, _ = split_pin(st.get("pin") or "")
        if add(inst, "path", st.get("delay"), st.get("cell")):
            resolved += 1
        else:
            unresolved += 1

    out = []
    for r in regions.values():
        r["cells"] = dict(r["cells"].most_common())
        r["delay_ns"] = round(r["delay_ns"], 5)
        r["source"] = rtl.span(r["file"], r["line"], r["end_line"])
        out.append(r)

    total_stage_delay = sum(float(s.get("delay") or 0.0) for s in stages)
    return {
        "regions": out,
        "coverage": {
            "stages_total": len(stages),
            "stages_localised": resolved,
            "stages_unresolved": unresolved,
            "fraction": round(resolved / len(stages), 3) if stages else 0.0,
            "delay_localised_ns": round(sum(r["delay_ns"] for r in out), 5),
            "delay_total_ns": round(total_stage_delay, 5),
        },
        "netlist_attributes_available": bool(nl.get("available")),
    }


# ---------------------------------------------------------------------------
# structural diagnosis
# ---------------------------------------------------------------------------

# Cell-name fragments, by what they imply about the RTL that produced them.
_FAMILIES: list[tuple[str, tuple[str, ...]]] = [
    ("adder",   ("FA_", "HA_", "FA_X", "HA_X", "ADD")),
    ("mux",     ("MUX", "MXI", "MX2")),
    ("xor",     ("XOR", "XNOR")),
    ("aoi_oai", ("AOI", "OAI")),
    ("nand_nor", ("NAND", "NOR")),
    ("inv_buf", ("INV", "BUF", "CLKBUF")),
    ("flop",    ("DFF", "SDFF", "LATCH")),
    ("and_or",  ("AND", "OR")),
]

HIGH_FANOUT = 8          # nangate45 X1 drivers start hurting around here
DEEP_LOGIC = 15          # combinational cells between flops
DELAY_CONCENTRATION = 0.5  # fraction of path delay in the top 5 stages


def _family(cell: str | None) -> str:
    up = (cell or "").upper()
    for name, frags in _FAMILIES:
        if any(f in up for f in frags):
            return name
    return "other"


def diagnose(path: dict[str, Any], period_ns: float | None = None) -> dict[str, Any]:
    """Grounded structural facts about one path, plus named root causes.

    Every finding carries the number that produced it, so the optimisation
    agent can be told to justify a rewrite against the evidence rather than
    against a label.
    """
    stages = path.get("stages") or []
    cells = [s for s in stages if s.get("cell")]
    fams = Counter(_family(s.get("cell")) for s in cells)
    delays = [(float(s.get("delay") or 0.0), s) for s in stages]
    total = sum(d for d, _ in delays) or 1e-12
    top5 = sorted(delays, key=lambda t: -t[0])[:5]

    fanouts = [(int(s["fanout"]), s) for s in stages if s.get("fanout") is not None]
    high_fo = sorted((t for t in fanouts if t[0] >= HIGH_FANOUT), key=lambda t: -t[0])[:6]

    depth = int(path.get("logic_depth") or 0)
    findings: list[dict[str, Any]] = []

    if depth >= DEEP_LOGIC:
        findings.append({
            "pattern": "deep combinational logic",
            "evidence": f"{depth} combinational cells between the launch and capture flops",
            "metric": {"logic_depth": depth, "threshold": DEEP_LOGIC},
        })

    if fams["adder"] >= 4 or (fams["xor"] + fams["aoi_oai"]) >= 12:
        findings.append({
            "pattern": "wide arithmetic / carry propagation",
            "evidence": (f"{fams['adder']} adder cells, {fams['xor']} XOR, "
                         f"{fams['aoi_oai']} AOI/OAI on one path -- a ripple or "
                         f"serially-chained datapath rather than a balanced tree"),
            "metric": {"adder": fams["adder"], "xor": fams["xor"],
                       "aoi_oai": fams["aoi_oai"]},
        })

    if fams["mux"] >= 4:
        findings.append({
            "pattern": "mux-heavy selection logic",
            "evidence": f"{fams['mux']} mux cells in series on the path",
            "metric": {"mux": fams["mux"]},
        })

    if high_fo:
        worst = high_fo[0]
        findings.append({
            "pattern": "high fanout on the critical path",
            "evidence": (f"fanout {worst[0]} at {worst[1].get('pin')} "
                         f"({len(high_fo)} stage(s) at or above {HIGH_FANOUT})"),
            "metric": {"max_fanout": worst[0], "threshold": HIGH_FANOUT,
                       "pins": [t[1].get("pin") for t in high_fo]},
        })

    concentration = sum(d for d, _ in top5) / total
    if concentration >= DELAY_CONCENTRATION and len(stages) > 5:
        findings.append({
            "pattern": "delay concentrated in a few stages",
            "evidence": (f"{concentration:.0%} of the path delay sits in 5 of "
                         f"{len(stages)} stages -- a local fix can recover most of it"),
            "metric": {"top5_fraction": round(concentration, 3)},
        })
    elif len(stages) > 5 and concentration < 0.25:
        findings.append({
            "pattern": "delay spread evenly along the path",
            "evidence": (f"the 5 worst of {len(stages)} stages hold only "
                         f"{concentration:.0%} of the delay -- no single hot cell; "
                         f"the depth itself is the problem"),
            "metric": {"top5_fraction": round(concentration, 3)},
        })

    slack = path.get("slack_ns")
    if period_ns and slack is not None and slack < 0:
        findings.append({
            "pattern": "slack gap",
            "evidence": (f"{abs(slack):.4f} ns short of a {period_ns:g} ns period "
                         f"({abs(slack) / period_ns:.0%} of the cycle)"),
            "metric": {"slack_ns": slack, "period_ns": period_ns,
                       "fraction_of_period": round(abs(slack) / period_ns, 3)},
        })

    return {
        "logic_depth": depth,
        "cell_families": dict(fams.most_common()),
        "cell_types": dict(Counter(s["cell"] for s in cells).most_common()),
        "stage_count": len(stages),
        "total_stage_delay_ns": round(total, 5),
        "top_stages": [
            {"delay_ns": d, "pin": s.get("pin"), "cell": s.get("cell"),
             "fanout": s.get("fanout")}
            for d, s in top5 if d > 0
        ],
        "findings": findings,
    }


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def analyse(timing: dict[str, Any], nl: dict[str, Any], rtl: RtlIndex,
            period_ns: float | None = None, top_k: int = 3) -> dict[str, Any]:
    """Top-k critical paths, localised and diagnosed. The paper's Sec. 4.1.2."""
    paths = timing.get("critical_paths") or []
    ranked = sorted(
        (p for p in paths if p.get("slack_ns") is not None),
        key=lambda p: p["slack_ns"],
    )[:top_k] or paths[:top_k]

    out = []
    for rank, p in enumerate(ranked, start=1):
        own = _period_of(period_ns, p.get("path_group"))
        out.append({
            "rank": rank,
            "startpoint": p.get("startpoint"),
            "endpoint": p.get("endpoint"),
            "slack_ns": p.get("slack_ns"),
            "status": p.get("status"),
            "clock_group": p.get("path_group"),
            "period_ns": own,
            "mapping": map_path(p, nl, rtl),
            "diagnosis": diagnose(p, own),
        })
    return {"top_k": len(out), "paths": out}


def _period_of(period_ns: Any, group: str | None) -> float | None:
    """Resolve one path's period.

    ``period_ns`` is either a ``clocks.ClockSet`` -- in which case the path's
    own capture clock decides -- or a bare float, which every single-clock
    caller passes and which applies to every path. Duck-typed so this module
    keeps its current import surface.
    """
    if hasattr(period_ns, "resolve_period"):
        return period_ns.resolve_period(group)[0]
    return period_ns


def render(analysis: dict[str, Any]) -> str:
    """Plain-text rendering -- what the Timing Analysis Agent hands downstream."""
    lines: list[str] = []
    for p in analysis.get("paths", []):
        cov = p["mapping"]["coverage"]
        lines.append(f"### Critical path {p['rank']}  "
                     f"slack {p['slack_ns']} ns ({p['status']})")
        lines.append(f"    {p['startpoint']} -> {p['endpoint']}")
        d = p["diagnosis"]
        lines.append(f"    logic depth {d['logic_depth']}, {d['stage_count']} stages, "
                     f"{d['total_stage_delay_ns']} ns of stage delay")
        lines.append(f"    localised {cov['stages_localised']}/{cov['stages_total']} "
                     f"stages ({cov['fraction']:.0%}) onto RTL")

        lines.append("")
        lines.append("    root causes:")
        for f in d["findings"]:
            lines.append(f"      - {f['pattern']}: {f['evidence']}")
        if not d["findings"]:
            lines.append("      - none of the structural heuristics fired")

        lines.append("")
        lines.append("    RTL regions on this path, in path order:")
        for r in p["mapping"]["regions"]:
            cells = ", ".join(f"{n}x{c}" for c, n in list(r["cells"].items())[:4]) or "-"
            lines.append(f"      {r['file']}:{r['line']}"
                         + (f"-{r['end_line']}" if r['end_line'] != r['line'] else "")
                         + f"  [{'/'.join(r['roles'])}]  {r['stages']} stage(s), "
                           f"{r['delay_ns']:.4f} ns  via {r['via']}")
            if r["signals"]:
                lines.append(f"          signals: {', '.join(r['signals'][:8])}")
            if r["cells"]:
                lines.append(f"          cells:   {cells}")
            for src_line in r["source"]:
                lines.append(f"        {src_line}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------


_STAGE_DIRS = {"sta": "02_sta", "pnr": "03_pnr"}


def from_run(rdir: Path, design_dir: Path | None = None,
             period_ns: float | None = None, top_k: int = 3,
             stage: str = "sta") -> dict[str, Any]:
    """Build the analysis from a finished run directory.

    ``stage`` picks which timing report to read. Both stages parse into the
    same shape -- ``astra.do_pnr`` runs the parsed output of the same Tcl
    reporting procs -- so everything downstream is indifferent. The netlist
    index stays at ``01_synth`` for both, because that JSON is the only
    structural source there is; post-PnR the localisation is correspondingly
    weaker, which the coverage fraction reports.
    """
    if stage not in _STAGE_DIRS:
        raise ValueError(f"unknown stage {stage!r}; expected one of "
                         f"{', '.join(sorted(_STAGE_DIRS))}")
    timing = json.loads((rdir / _STAGE_DIRS[stage] / "timing.json").read_text())
    nl = load_netlist_index(rdir / "01_synth" / "netlist.json")
    srcs = sorted((rdir / "00_inputs").glob("*.v")) + \
        sorted((rdir / "00_inputs").glob("*.sv"))
    if not srcs and design_dir:
        srcs = sorted((design_dir / "rtl").glob("*.v"))
    if period_ns is None:
        m = rdir / "metrics.json"
        if m.is_file():
            metrics = json.loads(m.read_text())
            import clocks
            period_ns = clocks.from_metrics(metrics) \
                or (metrics.get("clock") or {}).get("period_ns")
    return analyse(timing, nl, RtlIndex(srcs), period_ns, top_k)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: rtl_map.py <run_dir> [--json] [--top-k N]", file=sys.stderr)
        return 2
    rdir = Path(argv[1])
    top_k = int(argv[argv.index("--top-k") + 1]) if "--top-k" in argv else 3
    data = from_run(rdir, top_k=top_k)
    print(json.dumps(data, indent=2) if "--json" in argv else render(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
