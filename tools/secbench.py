#!/usr/bin/env python3
"""SEC on a fixed set of candidates, with no model calls.

Two jobs. It is the inner loop for work on the equivalence check itself --
seconds per candidate instead of a whole portfolio run -- and it is the
soundness gate: every case marked ``fail`` is a deliberately broken design,
and if any of them comes back equivalent the bench exits non-zero, because a
false pass would promote a broken candidate.

    python3 tools/secbench.py netproc \\
        --case good_t1:pass:results/netproc/pf-20260915-095208/iter_01/t1/rtl/netproc.v \\
        --case xnor:fail:benchmarks/sec_cases/netproc/m1_parity_xnor.v

A case is ``LABEL:EXPECT:PATH`` with EXPECT one of pass | fail | any. Results
go to ``runs/_secbench/<design>-<stamp>/results.json`` and a table on stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import clocks  # noqa: E402
import sec     # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def parse_case(text: str) -> tuple[str, str, Path]:
    label, expect, path = text.split(":", 2)
    if expect not in ("pass", "fail", "any"):
        raise argparse.ArgumentTypeError(f"{text}: expect must be pass|fail|any")
    return label, expect, Path(path)


def classify(expect: str, verdict: dict[str, Any]) -> str:
    """ok | FALSE-PASS | missed (a good candidate not certified) | info."""
    passed = verdict.get("equivalent") is True
    if expect == "fail":
        return "FALSE-PASS" if passed else "ok"
    if expect == "pass":
        return "ok" if passed else "missed"
    return "info"


def main() -> int:
    ap = argparse.ArgumentParser(prog="astra-secbench", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("design")
    ap.add_argument("--case", action="append", type=parse_case, default=[],
                    help="LABEL:EXPECT:PATH, repeatable")
    ap.add_argument("--gold", type=Path, nargs="+", default=None,
                    help="reference sources (default: the design's own RTL)")
    ap.add_argument("--depth", type=int, default=None)
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--regcorr-timeout", type=int, default=sec.REGCORR_TIMEOUT)
    ap.add_argument("--fallback-timeout", type=int, default=sec.FALLBACK_TIMEOUT)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    ddir = ROOT / "designs" / args.design
    cfg = json.loads((ddir / "config.json").read_text())
    gold = args.gold or [ddir / r for r in cfg["rtl"]]
    depth = args.depth if args.depth is not None else cfg.get("sec_depth", sec.DEFAULT_DEPTH)
    clock_set = clocks.ClockSet.from_config(cfg)
    out = args.out or (ROOT / "runs" / "_secbench"
                       / f"{args.design}-{datetime.now():%Y%m%d-%H%M%S}")
    out.mkdir(parents=True, exist_ok=True)

    print(f"[secbench] {args.design}: {len(args.case)} case(s), depth {depth}, "
          f"{len(clock_set)} clock(s), regcorr {args.regcorr_timeout}s, "
          f"fallback {args.fallback_timeout}s -> {out}")

    def run(case: tuple[str, str, Path]) -> dict[str, Any]:
        label, expect, path = case
        t0 = time.time()
        v = sec.check(cfg["top"], gold, [path], out / label, depth=depth,
                      clock_set=clock_set, regcorr_timeout=args.regcorr_timeout,
                      fallback_timeout=args.fallback_timeout)
        row = {"label": label, "expect": expect, "path": str(path),
               "wall_s": round(time.time() - t0, 1), "verdict": v,
               "outcome": classify(expect, v)}
        print(f"  {label:<24} expect {expect:<4} -> "
              f"{'EQUIV' if v.get('equivalent') else 'not  '} "
              f"{v.get('method', '?'):<17} {row['wall_s']:>7.1f}s  {row['outcome']}",
              flush=True)
        return row

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        rows = list(pool.map(run, args.case))

    (out / "results.json").write_text(json.dumps(rows, indent=2) + "\n")
    print(f"\n{'case':<24} {'expect':<6} {'verdict':<9} {'method':<17} "
          f"{'time':>8}  outcome / reason")
    for r in rows:
        v = r["verdict"]
        print(f"{r['label']:<24} {r['expect']:<6} "
              f"{'EQUIV' if v.get('equivalent') else 'not':<9} "
              f"{v.get('method', '?'):<17} {r['wall_s']:>7.1f}s  "
              f"{r['outcome']}: {str(v.get('reason', ''))[:110]}")

    false_passes = [r["label"] for r in rows if r["outcome"] == "FALSE-PASS"]
    if false_passes:
        print(f"\n[secbench] UNSOUND: {', '.join(false_passes)} reported equivalent")
        return 2
    missed = [r["label"] for r in rows if r["outcome"] == "missed"]
    print(f"\n[secbench] sound on {sum(r['expect'] == 'fail' for r in rows)} "
          f"broken case(s); {len(missed)} good case(s) not certified"
          + (f": {', '.join(missed)}" if missed else ""))
    return 1 if missed else 0


if __name__ == "__main__":
    raise SystemExit(main())
