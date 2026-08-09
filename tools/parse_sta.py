#!/usr/bin/env python3
"""Turn OpenSTA / OpenROAD stdout into structured JSON.

Two things come out of a flow stage's log:

  ``ASTRA_KV <key> <value>``          scalar metrics
  ``###ASTRA_BEGIN/END <section>``    delimited report text

and, from the ``*_paths`` sections, a list of critical paths broken down stage
by stage. That per-stage breakdown is the artifact the Timing Analysis Agent
consumes, so it gets parsed properly rather than shipped as a wall of text.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

# --------------------------------------------------------------------------
# markers
# --------------------------------------------------------------------------

_BEGIN = re.compile(r"^###ASTRA_BEGIN\s+(\S+)\s*$")
_END = re.compile(r"^###ASTRA_END\s+(\S+)\s*$")
_KV = re.compile(r"^ASTRA_KV\s+(\S+)\s+(.*)$")
_STEP = re.compile(r"^###ASTRA_STEP(_OK|_FAIL)?\s+(\S+)\s*(?::\s*(.*))?$")


def split_sections(text: str) -> dict[str, str]:
    """Collect every ``###ASTRA_BEGIN``/``END`` block, keyed by name."""
    out: dict[str, list[str]] = {}
    current: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        m = _BEGIN.match(line)
        if m:
            current, buf = m.group(1), []
            continue
        m = _END.match(line)
        if m and current == m.group(1):
            out.setdefault(current, []).extend(buf)
            current, buf = None, []
            continue
        if current is not None:
            buf.append(line)
    return {k: "\n".join(v).strip("\n") for k, v in out.items()}


def parse_kv(text: str) -> dict[str, Any]:
    """Collect ``ASTRA_KV`` scalars into a nested dict (dots become levels)."""
    flat: dict[str, Any] = {}
    for line in text.splitlines():
        m = _KV.match(line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2).strip()
        flat[key] = _coerce(key, raw)
    return _nest(flat)


def parse_steps(text: str) -> list[dict[str, Any]]:
    """PnR step outcomes, so a partial route is visible instead of silent."""
    steps: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for line in text.splitlines():
        m = _STEP.match(line)
        if not m:
            continue
        kind, name, msg = m.group(1), m.group(2), m.group(3)
        if name not in steps:
            steps[name] = {"name": name, "status": "started", "error": None}
            order.append(name)
        if kind == "_OK":
            steps[name]["status"] = "ok"
        elif kind == "_FAIL":
            steps[name]["status"] = "failed"
            steps[name]["error"] = (msg or "").strip()
    return [steps[n] for n in order]


def _coerce(key: str, raw: str) -> Any:
    if raw == "":
        return None
    # Slack in nanoseconds: OpenSTA's Tcl API hands back seconds and the Tcl
    # side multiplies by 1e9. If a build ever returns library units already,
    # the product is absurdly large — catch that rather than record garbage.
    try:
        val = float(raw)
    except ValueError:
        return raw
    if key.endswith("_ns") and abs(val) > 1e6:
        val = val / 1e9
    if raw.lstrip("-").isdigit():
        return int(raw)
    return val


def _nest(flat: dict[str, Any]) -> dict[str, Any]:
    root: dict[str, Any] = {}
    for key, val in flat.items():
        parts = key.split(".")
        node = root
        for p in parts[:-1]:
            nxt = node.get(p)
            if not isinstance(nxt, dict):
                nxt = {}
                node[p] = nxt
            node = nxt
        node[parts[-1]] = val
    return root


# --------------------------------------------------------------------------
# timing path reports
# --------------------------------------------------------------------------

_START = re.compile(r"^\s*Startpoint:\s+(\S+)(?:\s+\((.*)\))?")
_ENDPT = re.compile(r"^\s*Endpoint:\s+(\S+)(?:\s+\((.*)\))?")
_GROUP = re.compile(r"^\s*Path Group:\s+(\S+)")
_PTYPE = re.compile(r"^\s*Path Type:\s+(\S+)")
_SLACK = re.compile(r"slack\s*\((MET|VIOLATED)\)")
_HEADER = re.compile(r"^\s*(Fanout|Cap|Slew|Delay|Time|Description)\b")
_CELLREF = re.compile(r"\(([^)]+)\)\s*$")
_FLOAT = re.compile(r"-?\d+\.?\d*")


def _labeled_value(line: str, label: str) -> float | None:
    """Value belonging to a summary row such as ``2.54  2.54  data arrival time``.

    Summary rows carry one or two numeric columns before the label; the one
    that matters is always the last, so scan the text to the left of the label
    rather than anchoring on a fixed column count.
    """
    idx = line.find(label)
    if idx < 0:
        return None
    nums = _FLOAT.findall(line[:idx])
    if not nums:
        return None
    try:
        return float(nums[-1])
    except ValueError:
        return None


def _header_columns(line: str) -> list[str]:
    """Column names from a report_checks header, e.g.
    ``['fanout', 'cap', 'slew', 'delay', 'time', 'description']``."""
    return [tok.lower() for tok in line.split()]


def _num(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def _parse_stage(line: str, cols: list[str]) -> dict[str, Any] | None:
    """One row of a timing path.

    Column alignment in OpenSTA reports is not reliable enough to slice by
    character position — cells are blank when a field does not apply and wide
    values bleed into the neighbouring column's width. Instead: take the
    leading run of numeric tokens, and assign it *right-aligned* against the
    numeric column names, since Delay and Time are always the last two and are
    always present. A leading integer is Fanout, which disambiguates the
    remaining cases.
    """
    if not line.strip() or "description" not in cols:
        return None

    tokens = line.split()
    nums: list[float] = []
    for i, tok in enumerate(tokens):
        val = _num(tok)
        if val is None:
            tokens = tokens[i:]
            break
        nums.append(val)
    else:
        return None  # numbers only: a separator or summary row, not a stage

    desc = " ".join(tokens).strip()
    if not desc:
        return None

    numeric_cols = [c for c in cols if c != "description"]
    stage: dict[str, Any] = {c: None for c in numeric_cols}

    if nums:
        tail = numeric_cols[-2:]           # delay, time
        lead_cols = numeric_cols[:-2]      # fanout, cap, slew (any subset)
        for name, val in zip(tail, nums[-len(tail):] if len(nums) >= len(tail) else nums):
            stage[name] = val
        extras = nums[:-len(tail)] if len(nums) > len(tail) else []
        if extras and lead_cols:
            if lead_cols[0] == "fanout" and float(extras[0]).is_integer() \
                    and len(extras) == len(lead_cols):
                mapping = list(zip(lead_cols, extras))
            else:
                mapping = list(zip(lead_cols[-len(extras):], extras))
            for name, val in mapping:
                stage[name] = val
        if stage.get("fanout") is not None:
            stage["fanout"] = int(stage["fanout"])

    stage["description"] = desc
    stage["transition"] = None
    m = re.match(r"^([\^v])\s+(.*)$", desc)
    if m:
        stage["transition"] = "rise" if m.group(1) == "^" else "fall"
        desc = m.group(2)

    stage["cell"] = None
    stage["pin"] = desc.strip()
    cellref = _CELLREF.search(desc)
    if cellref:
        ref = cellref.group(1)
        pin = desc[: cellref.start()].strip()
        # "clock clk (rise edge)" is annotation, not an instance: a real cell
        # reference is a single identifier attached to a hierarchical pin.
        if " " not in ref and "/" in pin:
            stage["cell"] = ref
            stage["pin"] = pin
    return stage


def parse_paths(report: str) -> list[dict[str, Any]]:
    """Parse a ``report_checks`` block into a list of path dicts."""
    paths: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    spans: list[tuple[str, int, int]] = []

    def flush() -> None:
        nonlocal cur
        if cur is not None and (cur.get("startpoint") or cur.get("stages")):
            paths.append(cur)
        cur = None

    for line in report.splitlines():
        m = _START.match(line)
        if m:
            flush()
            cur = {
                "startpoint": m.group(1),
                "startpoint_info": m.group(2),
                "endpoint": None,
                "endpoint_info": None,
                "path_group": None,
                "path_type": None,
                "slack_ns": None,
                "status": None,
                "arrival_ns": None,
                "required_ns": None,
                "stages": [],
            }
            spans = []
            continue
        if cur is None:
            continue

        for pat, key in ((_ENDPT, "endpoint"), (_GROUP, "path_group"), (_PTYPE, "path_type")):
            m = pat.match(line)
            if m:
                cur[key] = m.group(1)
                if key == "endpoint":
                    cur["endpoint_info"] = m.group(2)
                break
        else:
            if _HEADER.match(line) and "Description" in line:
                spans = _header_columns(line)
                continue
            if set(line.strip()) <= {"-"} and line.strip():
                continue

            m = _SLACK.search(line)
            if m:
                cur["slack_ns"] = _labeled_value(line, "slack")
                cur["status"] = m.group(1)
                flush()
                continue
            if "data arrival time" in line:
                # The value repeats in the trailing summary block; keep the
                # first (signed as computed), not the negated summary copy.
                if cur.get("arrival_ns") is None:
                    cur["arrival_ns"] = _labeled_value(line, "data arrival time")
                # Everything past this point is the capture-clock/required
                # side of the report, not datapath stages.
                cur["_closed"] = True
                continue
            if "required time" in line:
                if cur.get("required_ns") is None:
                    cur["required_ns"] = _labeled_value(line, "required time")
                continue

            if spans and not cur.get("_closed"):
                stage = _parse_stage(line, spans)
                if stage:
                    cur["stages"].append(stage)

    flush()
    for p in paths:
        p.pop("_closed", None)
        p["logic_depth"] = _logic_depth(p["stages"])
    return paths


def _logic_depth(stages: list[dict[str, Any]]) -> int:
    """Combinational cells between the launching flop and the endpoint.

    Counting every cell would include the launch clock network, which inflates
    the number the Timing Analysis Agent reasons about. Start counting after
    the launch flop's Q pin when one is identifiable.
    """
    start = 0
    for i, s in enumerate(stages):
        pin = (s.get("pin") or "").upper()
        if pin.endswith("/Q") or pin.endswith("/QN"):
            start = i + 1
            break
    # The endpoint flop's own D pin is not a combinational stage.
    return sum(1 for s in stages[start:]
               if s.get("cell") and not (s.get("pin") or "").upper().endswith("/D"))


# --------------------------------------------------------------------------
# top level
# --------------------------------------------------------------------------


def parse_log(text: str, tag: str = "post_synth") -> dict[str, Any]:
    """Full structured view of one STA stage's log."""
    sections = split_sections(text)
    kv = parse_kv(text)
    paths = parse_paths(sections.get(f"{tag}_paths", ""))
    hold_paths = parse_paths(sections.get(f"{tag}_min_paths", ""))
    summary = kv.get(tag, {}) if isinstance(kv.get(tag), dict) else {}

    violating = [p for p in paths if p.get("status") == "VIOLATED"]
    if summary.get("wns_ns") is None and paths:
        slacks = [p["slack_ns"] for p in paths if p.get("slack_ns") is not None]
        if slacks:
            summary["wns_ns"] = min(slacks)

    return {
        "tag": tag,
        "summary": summary,
        "critical_paths": paths,
        "hold_paths": hold_paths,
        "worst_path": paths[0] if paths else None,
        "num_reported_paths": len(paths),
        "num_violating_reported": len(violating),
        "steps": parse_steps(text),
        "sections": sections,
    }


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: parse_sta.py <sta.log> [tag] [-o out.json]", file=sys.stderr)
        return 2
    tag = argv[2] if len(argv) > 2 and not argv[2].startswith("-") else "post_synth"
    with open(argv[1], encoding="utf-8", errors="replace") as fh:
        data = parse_log(fh.read(), tag)
    data.pop("sections", None)
    out = None
    if "-o" in argv:
        out = argv[argv.index("-o") + 1]
    text = json.dumps(data, indent=2)
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
