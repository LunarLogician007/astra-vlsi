#!/usr/bin/env python3
"""The clock data model: one design, many clocks.

The framework used to carry a single ``clock`` object -- one name, one period --
and every consumer divided by it. That is correct for a single-clock design and
silently wrong for anything else: a path captured by a 5 ns clock, normalised
against a 2 ns period, reports as far more critical than it is. Nothing raises,
nothing looks odd, and the portfolio spends its agent calls on the wrong cone
while reporting confident numbers. See docs/HANDOFF.md section 6.1.

So a period is never a free-floating float here. It belongs to a named clock,
and a path's period is looked up through the clock group that captured it.

This module is deliberately dependency-free -- stdlib only, no imports from the
rest of the tree -- so ``astra``, ``drrtl``, ``pathsel``, ``score`` and
``parse_sta`` can all resolve periods the same way without introducing an
import cycle.

Group naming follows OpenSTA: a path group is named after the clock that
captures the path, so a ``path_group`` from a parsed report is a clock name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

DEFAULT_CLOCK_NAME = "clk"
DEFAULT_PERIOD_NS = 10.0


@dataclass(frozen=True)
class Clock:
    """One clock. A generated clock knows what it divides and by how much."""

    name: str
    period_ns: float
    port: str | None = None
    generated_from: str | None = None
    divide_by: int | None = None

    @property
    def is_generated(self) -> bool:
        return self.generated_from is not None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "period_ns": self.period_ns}
        if self.port:
            out["port"] = self.port
        if self.generated_from:
            out["generated_from"] = self.generated_from
        if self.divide_by:
            out["divide_by"] = self.divide_by
        return out


class ClockError(ValueError):
    """A clock declaration that cannot be made sense of."""


class ClockSet:
    """Every clock in one design, in declaration order.

    ``primary`` is the first declared clock and is what the single-clock
    aliases resolve to. ``tightest`` is the shortest period, which is what the
    synthesis delay target uses -- see ``synthesis_period``.
    """

    def __init__(self, clocks: list[Clock]) -> None:
        if not clocks:
            raise ClockError("a design needs at least one clock")
        seen: set[str] = set()
        for c in clocks:
            if c.name in seen:
                raise ClockError(f"duplicate clock name {c.name!r}")
            seen.add(c.name)
            if not c.period_ns or c.period_ns <= 0:
                raise ClockError(f"clock {c.name!r} needs a positive period_ns")
        for c in clocks:
            if c.generated_from and c.generated_from not in seen:
                raise ClockError(
                    f"clock {c.name!r} is generated from {c.generated_from!r}, "
                    f"which is not declared")
        self._clocks = list(clocks)
        self._by_name = {c.name: c for c in clocks}

    # -- construction -------------------------------------------------------

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "ClockSet":
        """Build from a design config.

        Accepts the plural ``clocks`` list, or the singular ``clock`` object
        that every design shipped before multi-clock support. When both are
        present ``clocks`` wins, because it is the form that can express the
        whole design.
        """
        raw = cfg.get("clocks")
        if not raw:
            one = dict(cfg.get("clock") or {})
            one.setdefault("name", DEFAULT_CLOCK_NAME)
            one.setdefault("period_ns", DEFAULT_PERIOD_NS)
            raw = [one]
        if isinstance(raw, dict):        # a single object where a list belongs
            raw = [raw]
        decls = [dict(d) for d in raw]
        for d in decls:
            d.setdefault("name", DEFAULT_CLOCK_NAME)
        return cls([
            Clock(name=d["name"],
                  period_ns=period,
                  port=d.get("port") or (None if d.get("generated_from")
                                         else d["name"]),
                  generated_from=d.get("generated_from"),
                  divide_by=int(d["divide_by"]) if d.get("divide_by") else None)
            for d, period in zip(decls, cls._periods(decls))])

    @staticmethod
    def _periods(decls: list[dict[str, Any]]) -> list[float]:
        """Resolve every clock's period, following generated-from chains.

        A generated clock may state its ratio instead of its period, and its
        source may itself be a generated clock -- which is what a ripple
        divider is, and what the benchmark's divide-by-8 uses. So this
        iterates to a fixpoint rather than doing a single lookup: resolving
        only against clocks that carry an explicit period would fail on the
        second stage of any cascade.
        """
        known: dict[str, float] = {
            d["name"]: float(d["period_ns"]) for d in decls
            if d.get("period_ns") is not None}
        while True:
            progressed = False
            for d in decls:
                name, src, div = d["name"], d.get("generated_from"), d.get("divide_by")
                if name in known or not (src and div):
                    continue
                if src in known:
                    known[name] = known[src] * float(div)
                    progressed = True
            if not progressed:
                break

        missing = [d["name"] for d in decls if d["name"] not in known]
        if missing:
            raise ClockError(
                f"cannot determine a period for {', '.join(sorted(missing))}. "
                f"A clock needs either period_ns, or generated_from plus "
                f"divide_by resolving to a clock that has one (a cycle among "
                f"generated_from links will also land here)")
        return [known[d["name"]] for d in decls]

    # -- access -------------------------------------------------------------

    def __iter__(self) -> Iterator[Clock]:
        return iter(self._clocks)

    def __len__(self) -> int:
        return len(self._clocks)

    def __getitem__(self, i: int) -> Clock:
        return self._clocks[i]

    @property
    def primary(self) -> Clock:
        return self._clocks[0]

    @property
    def tightest(self) -> Clock:
        return min(self._clocks, key=lambda c: c.period_ns)

    @property
    def primary_period(self) -> float:
        return self.primary.period_ns

    @property
    def is_multi(self) -> bool:
        return len(self._clocks) > 1

    def by_name(self, name: str | None) -> Clock | None:
        return self._by_name.get(name) if name else None

    def names(self) -> list[str]:
        return [c.name for c in self._clocks]

    # -- the part that stops the silent mis-ranking -------------------------

    def resolve_period(self, group: str | None) -> tuple[float, bool]:
        """Period for a path group, and whether the lookup actually hit.

        Returns ``(period_ns, exact)``. On an unknown or absent group the
        period falls back to the primary clock and ``exact`` is False -- the
        caller is expected to carry that flag rather than drop it, because a
        fallback on a multi-clock design is exactly the condition that used to
        produce confidently wrong rankings.
        """
        c = self.by_name(group)
        if c is not None:
            return c.period_ns, True
        return self.primary.period_ns, not self.is_multi

    def period_for(self, group: str | None) -> float:
        """Period for a path group, fallback included. Use when the caller has
        already accounted for unresolved groups."""
        return self.resolve_period(group)[0]

    @property
    def synthesis_period(self) -> float:
        """The single delay target handed to ``abc -D``.

        Yosys maps the whole design in one pass and takes one delay target, so
        a multi-clock design cannot express a per-domain target here. The
        tightest period is used: it never under-constrains, so no domain is
        mapped too slowly to meet its own clock. The cost is that logic in a
        slow domain is optimised against a target it did not need, which can
        buy delay with area it did not need either. That bias is real and is
        reported rather than hidden -- see ``docs/multi-clock.md``.
        """
        return self.tightest.period_ns

    # -- grouping -----------------------------------------------------------

    def async_groups(self) -> list[list[str]]:
        """Clock names grouped by master. Separate groups are asynchronous.

        A generated clock is synchronous to the master it derives from, so it
        joins that master's group. Two independent master clocks have no known
        phase relationship, so paths between them are not real setup paths and
        must be excluded from analysis -- see ``render_clock_groups``.
        """
        groups: dict[str, list[str]] = {}
        for c in self._clocks:
            root = c.name
            seen: set[str] = set()
            while True:
                cur = self._by_name.get(root)
                if cur is None or not cur.generated_from or root in seen:
                    break
                seen.add(root)
                root = cur.generated_from
            groups.setdefault(root, []).append(c.name)
        return [groups[k] for k in sorted(groups, key=lambda n: self.names().index(n))]

    # -- serialisation ------------------------------------------------------

    def to_metrics(self) -> dict[str, Any]:
        """What goes into metrics.json.

        Both shapes are written: ``clocks`` is the whole truth, and ``clock``
        stays as a one-element alias so a reader that predates multi-clock
        support keeps working instead of reading ``None``.
        """
        return {
            "clock": {"name": self.primary.name,
                      "period_ns": self.primary.period_ns},
            "clocks": [c.to_dict() for c in self._clocks],
        }

    def with_period(self, period_ns: float | None) -> "ClockSet":
        """``--period`` override.

        Overriding one number on a multi-clock design is ambiguous, so it
        scales every clock by the same ratio against the primary. That keeps
        the ratios between domains -- which is what a divider means -- instead
        of collapsing them onto one number.
        """
        if period_ns is None:
            return self
        ratio = float(period_ns) / self.primary.period_ns
        return ClockSet([
            Clock(name=c.name, period_ns=c.period_ns * ratio, port=c.port,
                  generated_from=c.generated_from, divide_by=c.divide_by)
            for c in self._clocks])


# --------------------------------------------------------------------------
# reading a period back out of a finished run
# --------------------------------------------------------------------------


def from_metrics(metrics: dict[str, Any] | None) -> ClockSet | None:
    """Rebuild a ClockSet from a run's metrics.json. None if it says nothing."""
    if not metrics:
        return None
    if metrics.get("clocks"):
        try:
            return ClockSet.from_config({"clocks": metrics["clocks"]})
        except ClockError:
            return None
    if metrics.get("clock"):
        try:
            return ClockSet.from_config({"clock": metrics["clock"]})
        except ClockError:
            return None
    return None


# --------------------------------------------------------------------------
# SDC rendering
# --------------------------------------------------------------------------


def render_clock_defs(cs: ClockSet) -> str:
    """``create_clock`` / ``create_generated_clock`` for every clock."""
    lines: list[str] = []
    for c in cs:
        if c.is_generated:
            src = cs.by_name(c.generated_from)
            if src is None or not src.port:
                src_obj = f"[get_clocks {{{c.generated_from}}}]"
            elif src.is_generated:
                # A generated clock's own source is an internal pin, not a
                # port. Wrapping it in get_ports is how a cascaded divider
                # fails, and it fails at OpenSTA rather than here.
                src_obj = f"[get_pins {{{src.port}}}]"
            else:
                src_obj = f"[get_ports {{{src.port}}}]"
            target = (f"[get_pins {{{c.port}}}]" if c.port
                      else f"[get_ports {{{c.name}}}]")
            div = f" -divide_by {c.divide_by}" if c.divide_by else ""
            lines.append(f"create_generated_clock -name {c.name} "
                         f"-source {src_obj}{div} {target}")
        else:
            lines.append(f"create_clock -name {c.name} -period {c.period_ns:g} "
                         f"[get_ports {{{c.port or c.name}}}]")
    return "\n".join(lines)


def render_clock_groups(cs: ClockSet) -> str:
    """``set_clock_groups -asynchronous`` across independent masters.

    Without this every clock domain crossing is analysed as a setup path
    between clocks that have no phase relationship, which reports as an
    enormous violation. Those violations are not real, but they dominate TNS
    and would swamp the criticality distribution the portfolio ranks on --
    the selector would spend every agent call on a crossing that cannot be
    fixed in RTL. See docs/HANDOFF.md section 6.3.
    """
    groups = cs.async_groups()
    if len(groups) < 2:
        return ""
    parts = " ".join("-group {" + " ".join(g) + "}" for g in groups)
    return f"set_clock_groups -asynchronous {parts}"


def render_sdc_block(cs: ClockSet) -> str:
    """The whole generated constraint block, for ``@ASTRA_CLOCK_DEFS@``."""
    out = ["# --- generated by tools/clocks.py -------------------------------",
           render_clock_defs(cs)]
    groups = render_clock_groups(cs)
    if groups:
        out += ["", "# Independent masters have no phase relationship; crossings",
                "# between them are not setup paths.", groups]
    return "\n".join(out)


def _is_comment(line: str) -> bool:
    return line.lstrip().startswith("#")


def render_context(cs: ClockSet | None, fallback: float | None = None) -> str:
    """How the clocks are described to an agent, and in a run summary.

    A single-clock design reads exactly as it did before. A multi-clock design
    gets every clock listed with its period, because "target clock 2 ns" on a
    thirteen-clock design is not merely incomplete -- it contradicts the
    per-target brief, which names the clock that actually captures the path.
    An agent given both will believe one of them, and there is no reason it
    should pick the right one.
    """
    if cs is None:
        return f"target clock  {fallback:g} ns" if fallback else "target clock  n/a"
    if not cs.is_multi:
        return f"target clock  {cs.primary.period_ns:g} ns"
    rows = [f"  {c.name:<16} {c.period_ns:>7.3g} ns"
            + (f"   (÷{c.divide_by} of {c.generated_from})"
               if c.is_generated else "")
            for c in cs]
    return ("clocks ({} independent domains) -- each path is timed against ITS\n"
            "OWN clock, so a target's brief names which:\n{}".format(
                len(cs.async_groups()), "\n".join(rows)))


def substitute(text: str, cs: ClockSet) -> str:
    """Fill the clock placeholders in an SDC template.

        @CLK_PERIOD@            the primary clock's period  (back-compat)
        @CLK_PERIOD:<name>@     a named clock's period
        @ASTRA_CLOCK_DEFS@      create_clock/-generated + async groups

    A single-clock design that only uses ``@CLK_PERIOD@`` renders exactly as
    it did before, which is why the three shipped designs need no edit.

    **Comment lines are left alone.** SDC is line-oriented, and
    ``@ASTRA_CLOCK_DEFS@`` expands to many lines: substituting it inside a
    ``#`` comment uncomments everything after the first line and welds the
    rest of the comment onto the last generated command. Documenting the
    placeholder in a header comment is the obvious thing to do -- it is the
    first thing the benchmark's own SDC did -- so it must not corrupt the
    file.
    """
    out = []
    for line in text.splitlines(keepends=True):
        if _is_comment(line):
            out.append(line)
            continue
        if "@ASTRA_CLOCK_DEFS@" in line:
            line = line.replace("@ASTRA_CLOCK_DEFS@", render_sdc_block(cs))
        for c in cs:
            line = line.replace(f"@CLK_PERIOD:{c.name}@", f"{c.period_ns:g}")
        out.append(line.replace("@CLK_PERIOD@", f"{cs.primary.period_ns:g}"))
    return "".join(out)


def unresolved_placeholders(text: str) -> list[str]:
    """Any ``@CLK_PERIOD:<name>@`` left over -- i.e. naming a clock that does
    not exist. Left in the file it would reach OpenSTA as a syntax error with
    no hint of the cause.

    Comment lines are ignored, matching ``substitute``: a placeholder that is
    only ever mentioned in prose is documentation, not a broken reference.
    """
    import re
    live = "\n".join(ln for ln in text.splitlines() if not _is_comment(ln))
    return sorted(set(re.findall(r"@CLK_PERIOD:[^@]*@", live)))
