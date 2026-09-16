#!/usr/bin/env python3
"""Register correspondence -- sequential equivalence reduced to combinational.

The whole-design multi-clock check (flow/scripts/sec.tcl, `clk2fflogic`)
unrolls gold and gate together, so its cost follows the size of the design.
A candidate that rewrote one always-block of a 50K-cell benchmark pays for all
50K cells, twelve times over, and a correct one does not finish.

This module makes the cost follow the size of the *edit* instead.

The idea, in three steps
------------------------

1. **Pair registers by name.** A flop in the gold design and a flop in the
   candidate are the same register when they carry a common public name at the
   same bit (`xd_q[17]` on both sides). Yosys gives one net many names --
   `wire rot_in = xd_q` makes every bit of `xd_q` also a bit of `rot_in` -- so
   pairing uses every public alias of a bit, and only accepts a match that is
   unique in both directions. Anything else stays unpaired.

2. **Cut every paired flop open** ("evert" it). Its Q becomes a primary input
   shared by both sides, and five things about it become outputs that have to
   match: its next state D, its clock (`c`, inverted for a negedge flop), its
   reset-active signal (`r`, inverted for an active-low reset), and its reset
   value (`v`). An unpaired flop's Q is simply left undriven -- a free value
   the proof can assume nothing about.

3. **Prove the resulting purely combinational problem.** Most of it is
   textually identical on both sides, so an exact structural hash settles it
   without a solver: two bits that are built from the same cells, with the
   same parameters, over the same inputs, in the same order, are the same
   function. Only the ports whose structure differs are handed to Yosys
   (`equiv_make` / `equiv_simple`), and only their fan-in cones.

Why that is a proof for any clock interleaving
----------------------------------------------

Suppose every output port matches. Then for every paired register, its clock
signal, reset activity, reset value and next-state function are the same on
both sides *as functions of the paired Q values and the primary inputs*. Start
both designs in the same state (the same assumption the bounded miter makes
with `-set-init-zero`). Any event -- any clock edge on any domain, in any
order, or a reset -- updates the same registers on both sides to the same
values, so the states stay equal after it. By induction they are equal
forever, and so are the outputs, which are functions of that state. No clock
ratio is assumed and no depth is chosen: the claim is unbounded.

The crossings are not cut out of the claim. A synchroniser flop is a register
like any other and is paired and checked the same way; so is a clock divider,
whose Q then feeds other flops' `c` ports.

What it cannot do, reported rather than hidden
----------------------------------------------

Retiming moves registers, so correspondence by name fails and those cones stay
unproven. That is *undecided*, never a pass: `tools/sec.py` then falls back to
the whole-design bounded check. Nothing here can refute a candidate either --
an unproven port may be a real difference or a renamed register -- so a
refutation always comes from the bounded check.

Soundness does not rest on the pairing being right. A wrong pairing makes
obligations false, so they stay unproven. Only a complete proof passes.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

FLOPS = frozenset({"$dff", "$adff"})

# Yosys internal cells that are pure functions of their inputs. Anything not
# listed here and not in FLOPS -- latches, set/reset and async-load flops,
# memories, blackbox instances, formal cells -- makes the design unsupported
# rather than silently mis-modelled. A whitelist, deliberately: an unknown
# stateful cell must not be mistaken for a combinational one.
COMBINATIONAL = frozenset({
    "$not", "$pos", "$neg",
    "$and", "$or", "$xor", "$xnor",
    "$reduce_and", "$reduce_or", "$reduce_xor", "$reduce_xnor", "$reduce_bool",
    "$shl", "$shr", "$sshl", "$sshr", "$shift", "$shiftx",
    "$lt", "$le", "$eq", "$ne", "$eqx", "$nex", "$ge", "$gt",
    "$add", "$sub", "$mul", "$div", "$mod", "$divfloor", "$modfloor", "$pow",
    "$logic_not", "$logic_and", "$logic_or",
    "$mux", "$pmux", "$bmux", "$demux", "$bwmux",
    "$lut", "$sop", "$slice", "$concat", "$fa", "$lcu", "$alu", "$macc",
})

# Suffixes of the ports an everted register contributes. `@` cannot appear in
# a Verilog identifier, so these never collide with a name from the RTL.
Q, D, C, R, V = "@q", "@d", "@c", "@r", "@v"


class RegcorrError(Exception):
    """The design cannot be put through register correspondence at all."""


# ---------------------------------------------------------------------------
# reading the Yosys JSON
# ---------------------------------------------------------------------------


def load_module(path: Path, top: str) -> dict[str, Any]:
    doc = json.loads(Path(path).read_text())
    try:
        return doc["modules"][top]
    except KeyError:
        raise RegcorrError(f"{path}: no module {top!r} "
                           f"(found {sorted(doc.get('modules', {}))})") from None


def _int_param(value: Any) -> int:
    """A Yosys parameter as an integer. write_json emits bit strings."""
    if isinstance(value, int):
        return value
    text = str(value).strip()
    return int(text, 2) if text and set(text) <= {"0", "1"} else 0


def _bit_of(value: Any, index: int) -> str:
    """Bit `index` (LSB = 0) of a constant parameter, as "0"/"1"/"x"/"z"."""
    if isinstance(value, int):
        return str((value >> index) & 1)
    text = str(value)
    return text[-1 - index] if index < len(text) else "0"


def unsupported_cells(mod: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for cell in mod["cells"].values():
        if cell["type"] not in COMBINATIONAL and cell["type"] not in FLOPS:
            out[cell["type"]] += 1
    return dict(out)


def interface(mod: dict[str, Any]) -> dict[str, tuple[str, int]]:
    return {name: (p["direction"], len(p["bits"]))
            for name, p in mod["ports"].items()}


def public_aliases(mod: dict[str, Any]) -> dict[int, set[str]]:
    """Every public `name[index]` each net bit is known by."""
    out: dict[int, set[str]] = defaultdict(set)
    for name, net in mod["netnames"].items():
        if net.get("hide_name"):
            continue
        for i, bit in enumerate(net["bits"]):
            if isinstance(bit, int):
                out[bit].add(f"{name}[{i}]")
    return out


def flop_bits(mod: dict[str, Any]) -> dict[int, tuple[str, int]]:
    """Q bit -> (flop cell name, bit position within that cell)."""
    out: dict[int, tuple[str, int]] = {}
    for name, cell in mod["cells"].items():
        if cell["type"] in FLOPS:
            for i, bit in enumerate(cell["connections"]["Q"]):
                if isinstance(bit, int):
                    out[bit] = (name, i)
    return out


# ---------------------------------------------------------------------------
# 1. pairing
# ---------------------------------------------------------------------------


def pair_registers(gold: dict[str, Any], gate: dict[str, Any]
                   ) -> tuple[dict[int, str], dict[int, str], list[str], list[str]]:
    """Match flop bits across the two designs by a shared public name.

    Returns (gold Q bit -> pair name, gate Q bit -> pair name, unpaired gold
    labels, unpaired gate labels). A match is accepted only when it is unique
    in both directions: the gold bit's aliases lead to exactly one gate flop
    bit, and that bit's aliases lead back to exactly this gold bit.
    """
    gq, bq = flop_bits(gold), flop_bits(gate)
    ga, ba = public_aliases(gold), public_aliases(gate)

    gate_by_alias: dict[str, set[int]] = defaultdict(set)
    for q in bq:
        for a in ba.get(q, ()):
            gate_by_alias[a].add(q)
    gold_by_alias: dict[str, set[int]] = defaultdict(set)
    for q in gq:
        for a in ga.get(q, ()):
            gold_by_alias[a].add(q)

    gold_names: dict[int, str] = {}
    gate_names: dict[int, str] = {}
    for q in sorted(gq):
        partners: set[int] = set()
        for a in ga.get(q, ()):
            partners |= gate_by_alias.get(a, set())
        if len(partners) != 1:
            continue
        p = next(iter(partners))
        back: set[int] = set()
        for a in ba.get(p, ()):
            back |= gold_by_alias.get(a, set())
        if back != {q}:
            continue
        name = min(ga[q] & ba[p])
        gold_names[q] = name
        gate_names[p] = name

    def label(mod_q: dict[int, tuple[str, int]], aliases: dict[int, set[str]],
              q: int) -> str:
        return min(aliases[q]) if aliases.get(q) else "%s[%d]" % mod_q[q]

    unpaired_gold = sorted(label(gq, ga, q) for q in gq if q not in gold_names)
    unpaired_gate = sorted(label(bq, ba, q) for q in bq if q not in gate_names)
    return gold_names, gate_names, unpaired_gold, unpaired_gate


# ---------------------------------------------------------------------------
# 2. evert
# ---------------------------------------------------------------------------


def _max_bit(mod: dict[str, Any]) -> int:
    hi = 1
    for p in mod["ports"].values():
        hi = max([hi, *(b for b in p["bits"] if isinstance(b, int))])
    for c in mod["cells"].values():
        for bits in c["connections"].values():
            hi = max([hi, *(b for b in bits if isinstance(b, int))])
    for n in mod["netnames"].values():
        hi = max([hi, *(b for b in n["bits"] if isinstance(b, int))])
    return hi


def evert(mod: dict[str, Any], names: dict[int, str]) -> dict[str, Any]:
    """Cut every paired flop open. Unpaired flops vanish, leaving Q undriven.

    Each paired flop bit named N becomes:
        input  N@q  -> drives the old Q net
        output N@d  <- its D input
        output N@c  <- its clock, inverted if it is negedge-triggered
        output N@r  <- "reset is active": ARST, inverted if active-low; 0 for $dff
        output N@v  <- the reset value bit; 0 for $dff
    """
    new: dict[str, Any] = {
        "attributes": {},
        "ports": {k: dict(v) for k, v in mod["ports"].items()},
        "cells": {},
        "netnames": {k: v for k, v in mod["netnames"].items()},
    }
    nxt = [_max_bit(mod) + 1]
    count = [0]

    def fresh() -> int:
        nxt[0] += 1
        return nxt[0]

    def drive(src: Any, invert: bool = False, dst: int | None = None) -> int:
        dst = fresh() if dst is None else dst
        count[0] += 1
        new["cells"][f"$regcorr${count[0]}"] = {
            "hide_name": 1, "type": "$not" if invert else "$pos",
            "parameters": {"A_SIGNED": 0, "A_WIDTH": 1, "Y_WIDTH": 1},
            "attributes": {},
            "port_directions": {"A": "input", "Y": "output"},
            "connections": {"A": [src], "Y": [dst]},
        }
        return dst

    def port(name: str, direction: str, bit: int) -> None:
        if name in new["ports"]:
            raise RegcorrError(f"port name collision on {name!r}")
        new["ports"][name] = {"direction": direction, "bits": [bit]}

    for cname, cell in mod["cells"].items():
        if cell["type"] not in FLOPS:
            new["cells"][cname] = cell
            continue
        par, conn = cell["parameters"], cell["connections"]
        clk_inv = _int_param(par.get("CLK_POLARITY", 1)) == 0
        is_adff = cell["type"] == "$adff"
        rst_inv = is_adff and _int_param(par.get("ARST_POLARITY", 1)) == 0
        for i, q in enumerate(conn["Q"]):
            name = names.get(q) if isinstance(q, int) else None
            if name is None:
                continue
            qin = fresh()
            port(name + Q, "input", qin)
            drive(qin, dst=q)
            port(name + D, "output", drive(conn["D"][i]))
            port(name + C, "output", drive(conn["CLK"][0], invert=clk_inv))
            if is_adff:
                port(name + R, "output", drive(conn["ARST"][0], invert=rst_inv))
                port(name + V, "output", drive(_bit_of(par["ARST_VALUE"], i)))
            else:
                port(name + R, "output", drive("0"))
                port(name + V, "output", drive("0"))
    return new


# ---------------------------------------------------------------------------
# 3. exact structural hashing
# ---------------------------------------------------------------------------


def macc_terms(cell: dict[str, Any]) -> tuple[int, list[tuple[bool, bool, list, list]], list] | None:
    """Decode a `$macc` cell into (Y width, terms, single-bit addends).

    Each term is (signed, subtract, a bits, b bits): the value added is
    ±(a, or a*b when b is present), extended to the output width by its own
    signedness. Mirrors Yosys 0.33's Macc::from_cell -- CONFIG holds a 4-bit
    field width, then per term a signed bit, a subtract bit, and the widths of
    a and b; both operands are concatenated into port A in that order, and B
    holds one-bit addends. Returns None if anything fails to add up, so the
    caller falls back to the exact ordered key.
    """
    try:
        par, conn = cell["parameters"], cell["connections"]
        cfg = par["CONFIG"]
        if not isinstance(cfg, str) or not set(cfg) <= {"0", "1"}:
            return None
        bits = cfg[::-1]                               # index 0 = LSB
        if len(bits) < 4:
            return None
        nb = int(bits[:4][::-1], 2)
        a, pos, i, terms = conn["A"], 0, 4, []
        while i < len(bits):
            if i + 2 + 2 * nb > len(bits):
                return None
            signed, subtract = bits[i] == "1", bits[i + 1] == "1"
            size_a = int(bits[i + 2:i + 2 + nb][::-1] or "0", 2)
            size_b = int(bits[i + 2 + nb:i + 2 + 2 * nb][::-1] or "0", 2)
            i += 2 + 2 * nb
            in_a, in_b = a[pos:pos + size_a], a[pos + size_a:pos + size_a + size_b]
            pos += size_a + size_b
            terms.append((signed, subtract, in_a, in_b))
        if pos != len(a):
            return None
        return len(conn["Y"]), terms, list(conn.get("B", []))
    except (KeyError, TypeError, ValueError):
        return None


def macc_key(cell: dict[str, Any], bit_key) -> Any:
    """A `$macc` key that depends only on the value of the sum.

    Two normalisations, both arithmetic facts about a sum modulo 2^width:

      * Term order does not matter. `alumacc` folds a serial accumulate and a
        balanced tree over the same terms into one `$macc` each, differing only
        in that order.
      * A plain term is compared as its bits extended to the output width --
        padded with its top bit if signed, with 0 if not, and cut at the width,
        above which a sum mod 2^width cannot see. So a 32-bit signed operand
        and the same operand sign-extended by hand to 40 bits unsigned are the
        same term (measured: soc_bench's sys accumulate as a tree), while a
        zero-extended one is not.

    Everything else is exact: the subtract flag, every operand bit, and a
    product's two operands in order, with their signedness. This is the only
    rewriting the hash does, and it is arithmetic, not a guess about which
    inputs of a boolean cell commute.
    """
    decoded = macc_terms(cell)
    if decoded is None:
        return None
    width, terms, singles = decoded
    keyed = []
    for signed, subtract, ta, tb in terms:
        if tb:
            term = ("mul", signed, subtract, tuple(bit_key(b) for b in ta),
                    tuple(bit_key(b) for b in tb))
        else:
            bits = list(ta[:width])
            pad = ta[-1] if (signed and ta) else "0"
            bits += [pad] * (width - len(bits))
            term = ("add", subtract, tuple(bit_key(b) for b in bits))
        keyed.append(term)
    for b in singles:
        keyed.append(("add", False, tuple(bit_key(x) for x in [b] + ["0"] * (width - 1))))
    return ("$macc~", width, tuple(sorted(keyed, key=repr)))


class StructuralHash:
    """Hash-consing over both designs at once.

    Two bits get the same key exactly when they are built from the same cells,
    with the same parameters, over the same inputs, in the same port order.
    Nothing is normalised -- `a & b` and `b & a` get different keys -- so a
    shared key is always a real structural identity, and a missed one only
    sends a port to the solver that did not need it. Primary inputs are keyed
    by port name, which is what makes the two sides comparable. Undriven bits
    are keyed per side, so they never match anything.
    """

    def __init__(self) -> None:
        self.table: dict[Any, int] = {}

    def _intern(self, key: Any) -> int:
        return self.table.setdefault(key, len(self.table))

    def keys(self, mod: dict[str, Any], side: str):
        inputs: dict[int, Any] = {}
        for pname, p in mod["ports"].items():
            if p["direction"] == "input":
                for i, b in enumerate(p["bits"]):
                    if isinstance(b, int):
                        inputs[b] = ("in", pname, i)
        driver: dict[int, tuple[str, str, int]] = {}
        for cname, cell in mod["cells"].items():
            for pname, d in cell["port_directions"].items():
                if d == "output":
                    for i, b in enumerate(cell["connections"][pname]):
                        if isinstance(b, int):
                            driver[b] = (cname, pname, i)
        cells = mod["cells"]
        memo: dict[str, int] = {}

        def bit_key(b: Any) -> Any:
            if not isinstance(b, int):
                return ("const", b)
            if b in inputs:
                return inputs[b]
            d = driver.get(b)
            if d is None:
                return ("undriven", side, b)
            return ("out", cell_id(d[0]), d[1], d[2])

        def deps(cname: str) -> list[str]:
            cell = cells[cname]
            out = []
            for pname, d in cell["port_directions"].items():
                if d != "output":
                    for b in cell["connections"][pname]:
                        if isinstance(b, int) and b not in inputs and b in driver:
                            out.append(driver[b][0])
            return out

        def cell_id(root: str) -> int:
            if root in memo:
                return memo[root]
            # Iterative post-order: a 512-deep XOR chain would blow Python's
            # recursion limit.
            stack: list[tuple[str, bool]] = [(root, False)]
            onpath: set[str] = set()
            while stack:
                cname, done = stack.pop()
                if cname in memo:
                    continue
                if done:
                    onpath.discard(cname)
                    cell = cells[cname]
                    key = macc_key(cell, bit_key) if cell["type"] == "$macc" else None
                    if key is None:
                        ins = tuple(sorted(
                            (p, tuple(bit_key(b) for b in cell["connections"][p]))
                            for p, d in cell["port_directions"].items() if d != "output"))
                        params = tuple(sorted((k, str(v)) for k, v in
                                              cell.get("parameters", {}).items()))
                        key = (cell["type"], params, ins)
                    memo[cname] = self._intern(key)
                    continue
                if cname in onpath:
                    raise RegcorrError(f"combinational loop through cell {cname}")
                onpath.add(cname)
                stack.append((cname, True))
                for dep in deps(cname):
                    if dep not in memo:
                        if dep in onpath:
                            raise RegcorrError(
                                f"combinational loop through cell {dep}")
                        stack.append((dep, False))
            return memo[root]

        return bit_key


def differing_outputs(gold: dict[str, Any], gate: dict[str, Any]
                      ) -> tuple[list[str], int]:
    """Output ports whose structure is not identical. Returns (ports, total)."""
    sh = StructuralHash()
    gk, bk = sh.keys(gold, "gold"), sh.keys(gate, "gate")
    differ, total = [], 0
    for name, p in gold["ports"].items():
        if p["direction"] != "output":
            continue
        total += 1
        other = gate["ports"][name]["bits"]
        if any(gk(a) != bk(b) for a, b in zip(p["bits"], other)):
            differ.append(name)
    return differ, total


# ---------------------------------------------------------------------------
# cone extraction
# ---------------------------------------------------------------------------


def cone(mod: dict[str, Any], outputs: list[str]) -> dict[str, Any]:
    """Only what `outputs` depend on: their fan-in cells, every input port,
    and the public names that lie wholly inside the cone.

    A name that is only partly inside would leave its other bits undriven on
    both sides, and `equiv_make` would then ask for two free values to be
    equal -- unprovable, and a false "undecided".
    """
    driver: dict[int, str] = {}
    for cname, cell in mod["cells"].items():
        for pname, d in cell["port_directions"].items():
            if d == "output":
                for b in cell["connections"][pname]:
                    if isinstance(b, int):
                        driver[b] = cname
    keep_cells: set[str] = set()
    seen: set[int] = set()
    work = [b for o in outputs for b in mod["ports"][o]["bits"] if isinstance(b, int)]
    while work:
        b = work.pop()
        if b in seen:
            continue
        seen.add(b)
        cname = driver.get(b)
        if cname is None or cname in keep_cells:
            continue
        keep_cells.add(cname)
        cell = mod["cells"][cname]
        for bits in cell["connections"].values():
            work.extend(x for x in bits if isinstance(x, int) and x not in seen)
    ports = {k: v for k, v in mod["ports"].items()
             if v["direction"] == "input" or k in outputs}
    for p in ports.values():
        seen.update(b for b in p["bits"] if isinstance(b, int))
    netnames = {k: v for k, v in mod["netnames"].items()
                if not v.get("hide_name")
                and all((not isinstance(b, int)) or b in seen for b in v["bits"])}
    return {"attributes": {}, "ports": ports,
            "cells": {k: mod["cells"][k] for k in sorted(keep_cells)},
            "netnames": netnames}


def free_values(gold: dict[str, Any], gate: dict[str, Any]
                ) -> tuple[dict[str, Any], dict[str, Any], int, int]:
    """Make every value nothing drives an explicit input unique to its side.

    Undriven bits -- an unpaired register's Q, or a name a candidate used
    without ever driving -- and `x`/`z` constants inside a cone must be free:
    the solver may assume nothing about them. Left as they are, Yosys does not
    model them that way. Measured: a mechanical union that referenced an
    undeclared `acc_final` had its whole accumulate proved equal to the
    reference's, because gold's unpaired register values and the candidate's
    undriven wire compared equal. So each becomes a primary input named for
    its side, and each side is padded with the other's as unused ports, which
    keeps `equiv_make`'s port lists identical while no free value is shared.

    Returns (gold, gate, gold free count, gate free count).
    """
    def lift(mod: dict[str, Any], side: str) -> tuple[dict[str, Any], list[str]]:
        new = {"attributes": {}, "ports": {k: dict(v) for k, v in mod["ports"].items()},
               "cells": {k: dict(v, connections={p: list(b) for p, b in v["connections"].items()})
                         for k, v in mod["cells"].items()},
               "netnames": dict(mod["netnames"])}
        driven: set[int] = set()
        for p in new["ports"].values():
            if p["direction"] == "input":
                driven.update(b for b in p["bits"] if isinstance(b, int))
        for c in new["cells"].values():
            for pn, d in c["port_directions"].items():
                if d == "output":
                    driven.update(b for b in c["connections"][pn] if isinstance(b, int))
        nxt = [_max_bit(new) + 1]
        names: list[str] = []

        def free_port() -> int:
            nxt[0] += 1
            name = f"{side}@free{len(names)}"
            names.append(name)
            new["ports"][name] = {"direction": "input", "bits": [nxt[0]]}
            return nxt[0]

        lifted: dict[int, int] = {}
        for c in list(new["cells"].values()):
            for pn, d in c["port_directions"].items():
                if d == "output":
                    continue
                bits = c["connections"][pn]
                for i, b in enumerate(bits):
                    if isinstance(b, int) and b not in driven:
                        if b not in lifted:
                            lifted[b] = free_port()
                        bits[i] = lifted[b]
                    elif b in ("x", "z"):
                        bits[i] = free_port()      # every occurrence its own value
        for pname, p in list(new["ports"].items()):
            if p["direction"] != "output":
                continue
            for b in p["bits"]:
                if isinstance(b, int) and b not in driven and b not in lifted:
                    src = free_port()
                    lifted[b] = src
                    new["cells"][f"$regcorr$free{src}"] = {
                        "hide_name": 1, "type": "$pos", "attributes": {},
                        "parameters": {"A_SIGNED": 0, "A_WIDTH": 1, "Y_WIDTH": 1},
                        "port_directions": {"A": "input", "Y": "output"},
                        "connections": {"A": [src], "Y": [b]}}
                    driven.add(b)
            if any(b in ("x", "z") for b in p["bits"]):
                p["bits"] = [free_port() if b in ("x", "z") else b for b in p["bits"]]
        # A name that only covered now-lifted bits would ask equiv_make to match
        # two values that are free by construction; drop it.
        new["netnames"] = {k: v for k, v in new["netnames"].items()
                           if not any(isinstance(b, int) and b in lifted for b in v["bits"])}
        return new, names

    g, gn = lift(gold, "gold")
    b, bn = lift(gate, "gate")
    for mod, other in ((g, bn), (b, gn)):
        nxt = _max_bit(mod) + 1
        for i, name in enumerate(other):
            mod["ports"][name] = {"direction": "input", "bits": [nxt + 1 + i]}
    return g, b, len(gn), len(bn)


# ---------------------------------------------------------------------------
# the whole preparation step
# ---------------------------------------------------------------------------


def prepare(gold_json: Path, gate_json: Path, top: str, outdir: Path
            ) -> dict[str, Any]:
    """Pair, evert, hash, cut. Writes the SAT cones and returns a report.

    report["status"] is one of:
        proven       every output matched structurally; no solver needed
        needs_sat    report["sat_ports"] still differ; gold_cone.json and
                     gate_cone.json hold just their cones, modules gold/gate
        unsupported  report["reason"] says why; nothing was written
    """
    outdir.mkdir(parents=True, exist_ok=True)
    gold, gate = load_module(gold_json, top), load_module(gate_json, top)
    rep: dict[str, Any] = {"top": top}

    bad = {side: unsupported_cells(m) for side, m in (("gold", gold), ("gate", gate))}
    if any(bad.values()):
        rep.update(status="unsupported", unsupported_cells=bad,
                   reason="cells register correspondence cannot model: "
                          + "; ".join(f"{s} {dict(v)}" for s, v in bad.items() if v))
        return _write_report(outdir, rep)

    gi, bi = interface(gold), interface(gate)
    if gi != bi:
        diff = sorted(set(gi.items()) ^ set(bi.items()))
        rep.update(status="unsupported", interface_diff=[list(d) for d in diff],
                   reason=f"the port list differs: {diff[:6]}")
        return _write_report(outdir, rep)

    gnames, bnames, ug, ub = pair_registers(gold, gate)
    rep.update(flop_bits={"gold": len(flop_bits(gold)), "gate": len(flop_bits(gate))},
               paired=len(gnames), unpaired_gold=len(ug), unpaired_gate=len(ub),
               unpaired_examples={"gold": ug[:12], "gate": ub[:12]})

    try:
        gev, bev = evert(gold, gnames), evert(gate, bnames)
        differ, total = differing_outputs(gev, bev)
    except RegcorrError as e:
        rep.update(status="unsupported", reason=str(e))
        return _write_report(outdir, rep)

    rep.update(ports_total=total, ports_structural=total - len(differ),
               sat_ports=differ)
    if not differ:
        rep.update(status="proven",
                   reason=f"all {total} outputs of {len(gnames)} paired register "
                          f"bits and the module match structurally")
        return _write_report(outdir, rep)

    gc, bc = cone(gev, differ), cone(bev, differ)
    gc, bc, gfree, bfree = free_values(gc, bc)
    rep.update(status="needs_sat",
               free_values={"gold": gfree, "gate": bfree},
               sat_cells={"gold": len(gc["cells"]), "gate": len(bc["cells"])},
               reason=f"{len(differ)} of {total} outputs differ structurally "
                      f"and go to the solver")
    for name, mod in (("gold", gc), ("gate", bc)):
        (outdir / f"{name}_cone.json").write_text(json.dumps(
            {"creator": "astra regcorr", "modules": {name: mod}}))
    return _write_report(outdir, rep)


def _write_report(outdir: Path, rep: dict[str, Any]) -> dict[str, Any]:
    (outdir / "regcorr_report.json").write_text(json.dumps(rep, indent=2) + "\n")
    return rep


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="astra-regcorr",
        description="Pair registers, evert them, and cut the SAT cones "
                    "(the host-side step of the regcorr SEC stage).")
    ap.add_argument("top")
    ap.add_argument("--gold", type=Path, required=True, help="Yosys JSON, after proc; flatten")
    ap.add_argument("--gate", type=Path, required=True, help="Yosys JSON, after proc; flatten")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    try:
        rep = prepare(args.gold, args.gate, args.top, args.out)
    except RegcorrError as e:
        print(f"[regcorr] error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(rep, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
