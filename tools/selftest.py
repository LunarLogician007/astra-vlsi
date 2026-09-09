#!/usr/bin/env python3
"""Self-tests for the Dr. RTL layer: the paper's equations, the RTL mapper,
the skill library, and the agent-reply parsers.

None of this needs Yosys, OpenSTA or a model, which is the point -- the parts
that decide which rewrite wins should be checkable without a 1 GB container.

    python3 tools/selftest.py            # or: make selftest
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import drrtl        # noqa: E402
import merge        # noqa: E402
import pathsel      # noqa: E402
import portfolio    # noqa: E402
import rtl_map      # noqa: E402
import rtlscan      # noqa: E402
import score        # noqa: E402
import skillgen     # noqa: E402
import skills       # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# ===========================================================================
# Eq. 3 -- the scalar score
# ===========================================================================


class TestEq3(unittest.TestCase):
    """The published weights and the published normalisation."""

    BASE = {"wns_ns": -0.500, "tns_ns": -12.0, "area_um2": 1000.0}

    def test_paper_weights(self):
        w = score.DEFAULT_WEIGHTS
        self.assertEqual((w["alpha"], w["beta"], w["gamma"]), (0.50, 0.35, 0.15))
        self.assertEqual(w["area_penalty"], 0.50)
        self.assertEqual(w["area_threshold"], 0.10)

    def test_matches_paper_formula_in_its_regime(self):
        """While the baseline is negative, our form IS (x_i - x_b)/x_b."""
        for cand, base in ((-0.2, -0.5), (-0.8, -0.5), (-1.0, -12.0), (0.0, -0.5)):
            self.assertAlmostEqual(score.norm_timing(cand, base, 2.0),
                                   (cand - base) / base, places=12)

    def test_baseline_scores_zero(self):
        d = score.score(self.BASE, self.BASE, period_ns=2.0)
        self.assertAlmostEqual(d["score"], 0.0, places=12)

    def test_pure_timing_improvement_is_negative(self):
        cand = {"wns_ns": -0.25, "tns_ns": -6.0, "area_um2": 1000.0}
        d = score.score(cand, self.BASE, period_ns=2.0)
        # WNS halved: norm -0.5, weighted -0.25. TNS halved: -0.5 -> -0.175.
        self.assertAlmostEqual(d["normalized"]["wns"], -0.5, places=12)
        self.assertAlmostEqual(d["terms"]["wns"], -0.25, places=12)
        self.assertAlmostEqual(d["terms"]["tns"], -0.175, places=12)
        self.assertAlmostEqual(d["score"], -0.425, places=12)

    def test_regression_is_positive(self):
        cand = {"wns_ns": -1.0, "tns_ns": -24.0, "area_um2": 1000.0}
        self.assertGreater(score.score(cand, self.BASE, period_ns=2.0)["score"], 0)

    def test_area_penalty_threshold(self):
        """A flat 0.5 at area_norm > 0.10, and nothing at or below it."""
        at = {"wns_ns": -0.5, "tns_ns": -12.0, "area_um2": 1100.0}   # exactly +10%
        over = {"wns_ns": -0.5, "tns_ns": -12.0, "area_um2": 1100.1}
        self.assertEqual(score.score(at, self.BASE, period_ns=2.0)["terms"]["penalty"], 0.0)
        self.assertEqual(score.score(over, self.BASE, period_ns=2.0)["terms"]["penalty"], 0.5)

    def test_penalty_can_outweigh_a_timing_win(self):
        """The paper's stated purpose: discourage excessive area overhead."""
        bloated = {"wns_ns": -0.30, "tns_ns": -8.0, "area_um2": 1600.0}
        d = score.score(bloated, self.BASE, period_ns=2.0)
        self.assertGreater(d["score"], 0.0)
        self.assertEqual(d["terms"]["penalty"], 0.5)

    def test_missing_metric_is_neutral_not_favourable(self):
        d = score.score({"wns_ns": -0.5, "area_um2": 1000.0}, self.BASE, period_ns=2.0)
        self.assertEqual(d["normalized"]["tns"], 0.0)

    def test_closed_baseline_still_rewards_more_slack(self):
        """Outside the paper's regime the sign must not invert."""
        base = {"wns_ns": 0.10, "tns_ns": 0.0, "area_um2": 1000.0}
        better = {"wns_ns": 0.30, "tns_ns": 0.0, "area_um2": 1000.0}
        worse = {"wns_ns": 0.05, "tns_ns": 0.0, "area_um2": 1000.0}
        self.assertLess(score.score(better, base, period_ns=2.0)["score"], 0)
        self.assertGreater(score.score(worse, base, period_ns=2.0)["score"], 0)

    def test_zero_baseline_does_not_divide_by_zero(self):
        base = {"wns_ns": 0.0, "tns_ns": 0.0, "area_um2": 1000.0}
        d = score.score({"wns_ns": 0.2, "tns_ns": 0.0, "area_um2": 1000.0},
                        base, period_ns=2.0)
        self.assertTrue(-10 < d["score"] < 10)


# ===========================================================================
# Eq. 4 -- selection under the SEC constraint
# ===========================================================================


class TestEq4(unittest.TestCase):
    def test_picks_lowest_score(self):
        g = [{"id": "a", "score": -0.1, "sec": {"equivalent": True}},
             {"id": "b", "score": -0.9, "sec": {"equivalent": True}},
             {"id": "c", "score": 0.4, "sec": {"equivalent": True}}]
        self.assertEqual(score.select_best(g)["id"], "b")

    def test_sec_failure_cannot_win(self):
        """The whole point of the constraint: a broken rewrite is not a result."""
        g = [{"id": "fast_but_wrong", "score": -5.0, "sec": {"equivalent": False}},
             {"id": "correct", "score": -0.1, "sec": {"equivalent": True}}]
        self.assertEqual(score.select_best(g)["id"], "correct")

    def test_unrun_sec_is_not_a_pass(self):
        g = [{"id": "unchecked", "score": -5.0},
             {"id": "checked", "score": -0.1, "sec": {"equivalent": True}}]
        self.assertEqual(score.select_best(g)["id"], "checked")

    def test_no_eligible_candidate_returns_none(self):
        self.assertIsNone(score.select_best(
            [{"id": "x", "score": -1.0, "sec": {"equivalent": False}}]))
        self.assertIsNone(score.select_best([]))

    def test_unscored_candidate_is_skipped(self):
        g = [{"id": "crashed", "score": None, "sec": {"equivalent": True}},
             {"id": "ok", "score": 0.2, "sec": {"equivalent": True}}]
        self.assertEqual(score.select_best(g)["id"], "ok")


# ===========================================================================
# Eq. 5 -- group-relative advantage
# ===========================================================================


class TestEq5(unittest.TestCase):
    def sec_ok(self, *scores):
        return [{"id": f"c{i}", "score": s, "sec": {"equivalent": True}}
                for i, s in enumerate(scores)]

    def test_is_a_population_z_score(self):
        g = score.advantages(self.sec_ok(-1.0, 0.0, 1.0))
        self.assertAlmostEqual(sum(c["advantage"] for c in g), 0.0, places=12)
        # mean 0, population sigma sqrt(2/3)
        self.assertAlmostEqual(g[0]["advantage"], -1.2247448713915889, places=10)
        self.assertAlmostEqual(g[2]["advantage"], +1.2247448713915889, places=10)

    def test_better_candidate_gets_negative_advantage(self):
        """Scores are minimised, so the winner is the one below the mean."""
        g = score.advantages(self.sec_ok(-0.8, -0.1, 0.3))
        self.assertLess(g[0]["advantage"], 0)
        self.assertGreater(g[2]["advantage"], 0)

    def test_degenerate_group_yields_zero_not_infinity(self):
        for g in (self.sec_ok(0.5), self.sec_ok(0.5, 0.5, 0.5)):
            for c in score.advantages(g):
                self.assertEqual(c["advantage"], 0.0)

    def test_sec_failures_excluded_from_mu_and_sigma(self):
        g = [{"id": "a", "score": -1.0, "sec": {"equivalent": True}},
             {"id": "b", "score": +1.0, "sec": {"equivalent": True}},
             {"id": "junk", "score": -99.0, "sec": {"equivalent": False}}]
        score.advantages(g)
        self.assertIsNone(g[2]["advantage"])
        # mu is 0 over {-1, +1}; the outlier did not drag it.
        self.assertAlmostEqual(g[0]["advantage"], -1.0, places=12)
        self.assertAlmostEqual(g[1]["advantage"], +1.0, places=12)

    def test_scale_invariance(self):
        """A z-score must not care that one design's slacks are 10x another's."""
        a = [c["advantage"] for c in score.advantages(self.sec_ok(-1.0, 0.0, 2.0))]
        b = [c["advantage"] for c in score.advantages(self.sec_ok(-10.0, 0.0, 20.0))]
        for x, y in zip(a, b):
            self.assertAlmostEqual(x, y, places=12)

    def test_score_group_end_to_end(self):
        base = {"wns_ns": -0.5, "tns_ns": -12.0, "area_um2": 1000.0}
        g = [
            {"id": "good", "sec": {"equivalent": True},
             "metrics": {"wns_ns": -0.2, "tns_ns": -4.0, "area_um2": 1010.0}},
            {"id": "bloat", "sec": {"equivalent": True},
             "metrics": {"wns_ns": -0.1, "tns_ns": -2.0, "area_um2": 1500.0}},
            {"id": "broken", "sec": {"equivalent": False},
             "metrics": {"wns_ns": -0.05, "tns_ns": -1.0, "area_um2": 1000.0}},
            {"id": "crashed", "sec": {"equivalent": True},
             "metrics": {"status": "synth_failed"}},
        ]
        score.score_group(g, base, period_ns=2.0)
        self.assertIsNone(g[3]["score"])
        self.assertIsNone(g[3]["advantage"])
        self.assertEqual(g[1]["score_detail"]["terms"]["penalty"], 0.5)
        self.assertEqual(score.select_best(g)["id"], "good")


# ===========================================================================
# skill library
# ===========================================================================


class TestSkills(unittest.TestCase):
    def lib(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        return skills.SkillLibrary(Path(self.tmp.name) / "library.json")

    def test_seed_starts_with_no_confidence(self):
        lib = self.lib()
        e = lib.add_seed("wide mux", "flatten the select")
        self.assertEqual(e["confidence"], 0.0)
        self.assertEqual(e["stats"]["occurrences"], 0)

    def test_confidence_rises_with_repeated_wins(self):
        lib = self.lib()
        prev = -1.0
        for i in range(6):
            e = lib.record("serial chain", "adder tree", sec_pass=True,
                           advantage=-1.0, design="d", iteration=i)
            self.assertGreater(e["confidence"], prev)
            prev = e["confidence"]
        self.assertEqual(e["status"], "effective")

    def test_sec_failures_condemn_an_entry(self):
        lib = self.lib()
        for i in range(4):
            e = lib.record("bad idea", "drop a pipeline stage", sec_pass=False,
                           advantage=None, design="d", iteration=i)
        self.assertEqual(e["status"], "invalid")
        self.assertEqual(e["confidence"], 0.0)
        self.assertNotIn(e["id"], [x["id"] for x in lib.all()])
        self.assertIn(e["id"], [x["id"] for x in lib.all(include_invalid=True)])

    def test_a_single_lucky_trial_stays_provisional(self):
        lib = self.lib()
        e = lib.record("p", "s", sec_pass=True, advantage=-3.0,
                       design="d", iteration=0)
        self.assertLess(e["confidence"], skills.EFFECTIVE_AT)
        self.assertEqual(e["status"], "candidate")

    def test_losing_advantage_lowers_confidence(self):
        lib = self.lib()
        for i in range(5):
            win = lib.record("serial accumulate chain", "rebalance into a tree",
                             sec_pass=True, advantage=-1.2, design="d", iteration=i)
            lose = lib.record("high fanout control signal",
                              "insert a buffer manually",
                              sec_pass=True, advantage=+1.2, design="d", iteration=i)
        self.assertEqual(len(lib), 2)
        self.assertGreater(win["confidence"], lose["confidence"])

    def test_failed_candidates_do_not_contribute_advantage(self):
        lib = self.lib()
        e = lib.record("p", "s", sec_pass=False, advantage=-9.0,
                       design="d", iteration=0)
        self.assertEqual(e["stats"]["advantage_n"], 0)

    def test_similar_phrasings_merge_into_one_entry(self):
        lib = self.lib()
        lib.record("serial accumulate chain", "rebalance into an adder tree",
                   sec_pass=True, advantage=-1.0, design="d", iteration=0)
        lib.record("serial accumulate chain of adds",
                   "rebalance the chain into a balanced adder tree",
                   sec_pass=True, advantage=-1.0, design="e", iteration=1)
        self.assertEqual(len(lib), 1)
        self.assertEqual(next(iter(lib.entries.values()))["stats"]["occurrences"], 2)

    def test_unrelated_entries_do_not_merge(self):
        lib = self.lib()
        lib.record("high fanout control signal", "replicate the driver",
                   sec_pass=True, advantage=-1.0, design="d", iteration=0)
        lib.record("wide comparison at end of datapath", "precompute in parallel",
                   sec_pass=True, advantage=-1.0, design="d", iteration=0)
        self.assertEqual(len(lib), 2)

    def test_match_finds_by_wording_overlap(self):
        lib = self.lib()
        lib.add_seed("high-fanout signal on the critical path",
                     "replicate the driver")
        hits = lib.match(["high fanout on the critical path"])
        self.assertEqual(len(hits), 1)

    def test_round_trip_persists_statistics(self):
        lib = self.lib()
        lib.record("p", "s", sec_pass=True, advantage=-0.5, design="d", iteration=0)
        path = lib.save()
        again = skills.SkillLibrary(path)
        self.assertEqual(len(again), 1)
        self.assertEqual(next(iter(again.entries.values()))["stats"]["occurrences"], 1)

    def test_shipped_library_is_honest(self):
        """Seeds must not ship with invented evidence behind them."""
        shipped = Path(__file__).resolve().parent.parent / "skills" / "library.json"
        if not shipped.is_file():
            self.skipTest("no shipped library")
        lib = skills.SkillLibrary(shipped)
        for e in lib.all(include_invalid=True):
            if e.get("source") == "seed":
                self.assertEqual(e["stats"]["occurrences"], 0, e["id"])
                self.assertEqual(e["confidence"], 0.0, e["id"])


# ===========================================================================
# path -> RTL mapping
# ===========================================================================

_RTL = """\
module m (input clk, input signed [15:0] a0, output reg [39:0] acc_out);
    reg signed [15:0] a0_q;
    wire signed [39:0] s0 = a0_q * a0_q;
    wire signed [39:0] s1 = s0 + s0;
    always @(posedge clk) acc_out <= s1;
endmodule
"""


class TestRtlMap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.f = Path(self.tmp.name) / "m.v"
        self.f.write_text(_RTL)
        self.rtl = rtl_map.RtlIndex([self.f])

    def test_parse_src_attribute(self):
        got = rtl_map.parse_src("mac_chain.v:66.29-68.36")
        self.assertEqual(got, [{"file": "mac_chain.v", "line": 66, "end_line": 68}])

    def test_parse_multi_span_src(self):
        self.assertEqual(len(rtl_map.parse_src("a.v:1.1-1.9|a.v:4.1-4.9")), 2)

    def test_base_ident_strips_bit_select(self):
        self.assertEqual(rtl_map.base_ident("a0_q[3]"), "a0_q")
        self.assertEqual(rtl_map.base_ident("top.u1.s2[15:0]"), "s2")

    def test_recognises_write_verilog_mangling(self):
        self.assertTrue(rtl_map.is_mangled("_14637_"))
        self.assertTrue(rtl_map.is_mangled("_14637_[2]"))
        self.assertFalse(rtl_map.is_mangled("a0_q"))

    def test_split_pin(self):
        self.assertEqual(rtl_map.split_pin("a0_q[3]/Q"), ("a0_q[3]", "Q"))
        self.assertEqual(rtl_map.split_pin("s2[9]"), ("s2[9]", None))

    def test_identifier_lookup_prefers_the_driver(self):
        hits = self.rtl.find_identifier("s1")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["kind"], "driver")
        self.assertEqual(hits[0]["line"], 4)

    def test_maps_a_path_without_netlist_attributes(self):
        """The regex fallback has to carry the case where src is gone."""
        path = {
            "startpoint": "a0_q[3]", "endpoint": "acc_out[9]",
            "logic_depth": 20,
            "stages": [
                {"pin": "a0_q[3]/Q", "cell": "DFF_X1", "delay": 0.09, "fanout": 2},
                {"pin": "s0[9]", "cell": None, "delay": 0.0, "fanout": 1},
                {"pin": "_14637_/ZN", "cell": "AOI21_X1", "delay": 0.05, "fanout": 3},
                {"pin": "s1[9]", "cell": None, "delay": 0.02, "fanout": 1},
                {"pin": "acc_out[9]/D", "cell": "DFF_X1", "delay": 0.0, "fanout": 1},
            ],
        }
        m = rtl_map.map_path(path, {"cells": {}, "nets": {}}, self.rtl)
        files = {r["file"] for r in m["regions"]}
        self.assertEqual(files, {"m.v"})
        lines = {r["line"] for r in m["regions"]}
        self.assertTrue({2, 3, 4}.issubset(lines))     # a0_q, s0, s1 all found
        self.assertEqual(m["coverage"]["stages_unresolved"], 1)  # the _14637_ one

    def test_netlist_src_attribute_wins_over_regex(self):
        nl = {"cells": {}, "nets": {"s1": {"src": [{"file": "m.v", "line": 4,
                                                    "end_line": 4}]}},
              "available": True}
        got = rtl_map.resolve("s1[9]", nl, self.rtl)
        self.assertEqual(got[0]["via"], "src attribute (net)")

    def test_constant_rhs_is_recognised_as_a_reset_branch(self):
        const = ["q <= 0;", "acc_out   <= {ACC{1'b0}};", "sat_flag <= 1'b0;",
                 "q <= {W{1'b0}};", "valid <= 1'd0;"]
        real = ["acc_out <= sat_val;", "q <= a + b;", "valid_out <= valid_q;",
                "wire [39:0] s1 = s0 + p1;"]
        for t in const:
            self.assertTrue(rtl_map.RtlIndex._is_constant_assign(t), t)
        for t in real:
            self.assertFalse(rtl_map.RtlIndex._is_constant_assign(t), t)

    def test_reset_branch_ranks_below_the_real_driver(self):
        f = Path(self.tmp.name) / "r.v"
        f.write_text("module r(input clk, output reg [3:0] q);\n"
                     "  always @(posedge clk)\n"
                     "    if (!rst_n) q <= 4'b0;\n"
                     "    else q <= q + 1;\n"
                     "endmodule\n")
        hits = rtl_map.RtlIndex([f]).find_identifier("q")
        self.assertEqual(hits[0]["line"], 4)

    def test_using_a_signal_is_not_declaring_it(self):
        """`wire p0 = a0_q * b0_q;` declares p0, not a0_q."""
        for h in self.rtl.find_identifier("a0_q"):
            self.assertNotEqual(h["line"], 3, "line 3 declares s0, not a0_q")

    def test_ambiguous_regex_hits_do_not_multiply_the_delay(self):
        """One stage's delay must land on one region, not on every mention."""
        path = {"startpoint": "a0_q[0]", "endpoint": "acc_out[0]",
                "logic_depth": 1,
                "stages": [{"pin": "a0_q[0]/Q", "cell": "DFF_X1",
                            "delay": 0.09, "fanout": 2}]}
        m = rtl_map.map_path(path, {"cells": {}, "nets": {}}, self.rtl)
        self.assertAlmostEqual(
            sum(r["delay_ns"] for r in m["regions"]), 0.09, places=6)

    def test_endpoint_registration_is_not_counted_as_a_stage(self):
        path = {"startpoint": "a0_q[0]", "endpoint": "acc_out[0]",
                "logic_depth": 0, "stages": []}
        m = rtl_map.map_path(path, {"cells": {}, "nets": {}}, self.rtl)
        self.assertTrue(m["regions"])
        self.assertEqual(sum(r["stages"] for r in m["regions"]), 0)

    def test_strips_autoname_cell_chains(self):
        cases = {
            "acc_out[33]_DFF_X1_Q": "acc_out[33]",
            "b1[0]_AND2_X1_A2_ZN_DFF_X1_D": "b1[0]",
            "acc_out[12]_DFF_X1_Q_D_AOI21_X1_ZN_B2_XNOR2_X1_ZN": "acc_out[12]",
            "a0_q[3]": "a0_q[3]",          # no chain: unchanged
            "s2": "s2",
        }
        for name, want in cases.items():
            self.assertEqual(rtl_map.strip_autoname(name), want, name)

    def test_autoname_chain_resolves_to_its_origin_net(self):
        got = rtl_map.resolve("acc_out[9]_DFF_X1_Q_D_AOI21_X1_ZN",
                              {"cells": {}, "nets": {}}, self.rtl)
        self.assertTrue(got)
        self.assertIn("cone origin", got[0]["via"])

    def test_full_name_wins_over_its_autoname_prefix(self):
        """`bus_A0` must resolve to itself, not be truncated to `bus`."""
        f = Path(self.tmp.name) / "u.v"
        f.write_text("module u(input clk);\n"
                     "  wire bus = 1'b0;\n"
                     "  wire bus_A0 = clk;\n"
                     "endmodule\n")
        idx = rtl_map.RtlIndex([f])
        got = rtl_map.resolve("bus_A0", {"cells": {}, "nets": {}}, idx)
        self.assertEqual(got[0]["line"], 3)
        self.assertNotIn("cone origin", got[0]["via"])

    def test_signal_list_holds_rtl_names_not_autoname_chains(self):
        chain = ("acc_out[9]_DFF_X1_Q_D_AND3_X1_ZN_A2_OR3_X1_ZN_A1"
                 "_AND3_X1_ZN_A1_OR3_X1_ZN_A1_AOI22_X1_ZN")
        path = {"startpoint": "a0_q[0]", "endpoint": "acc_out[9]",
                "logic_depth": 3,
                "stages": [{"pin": chain + "/ZN", "cell": "AOI22_X1",
                            "delay": 0.05, "fanout": 2}]}
        m = rtl_map.map_path(path, {"cells": {}, "nets": {}}, self.rtl)
        for r in m["regions"]:
            for sig in r["signals"]:
                self.assertLess(len(sig), 40, sig)
                self.assertNotIn("_X1_", sig)

    def test_diagnose_flags_deep_logic_and_fanout(self):
        path = {"logic_depth": 30, "slack_ns": -0.8,
                "stages": [{"pin": f"_{i}_/ZN", "cell": "AOI21_X1",
                            "delay": 0.02, "fanout": 2} for i in range(30)]
                + [{"pin": "big/ZN", "cell": "INV_X1", "delay": 0.4, "fanout": 24}]}
        d = rtl_map.diagnose(path, period_ns=2.0)
        pats = {f["pattern"] for f in d["findings"]}
        self.assertIn("deep combinational logic", pats)
        self.assertIn("high fanout on the critical path", pats)
        self.assertIn("slack gap", pats)

    def test_diagnose_flags_serial_arithmetic(self):
        path = {"logic_depth": 12,
                "stages": [{"pin": f"_{i}_/S", "cell": "FA_X1", "delay": 0.05,
                            "fanout": 1} for i in range(8)]}
        pats = {f["pattern"] for f in rtl_map.diagnose(path)["findings"]}
        self.assertIn("wide arithmetic / carry propagation", pats)

    def test_diagnose_distinguishes_hot_cell_from_deep_path(self):
        even = {"logic_depth": 20, "stages": [
            {"pin": f"_{i}_/ZN", "cell": "NAND2_X1", "delay": 0.05, "fanout": 2}
            for i in range(40)]}
        pats = {f["pattern"] for f in rtl_map.diagnose(even)["findings"]}
        self.assertIn("delay spread evenly along the path", pats)

        hot = {"logic_depth": 6, "stages": [
            {"pin": f"_{i}_/ZN", "cell": "NAND2_X1",
             "delay": 0.9 if i == 0 else 0.01, "fanout": 2} for i in range(20)]}
        pats = {f["pattern"] for f in rtl_map.diagnose(hot)["findings"]}
        self.assertIn("delay concentrated in a few stages", pats)


# ===========================================================================
# agent reply parsing
# ===========================================================================


class TestReplyParsing(unittest.TestCase):
    def test_fenced_json(self):
        got = drrtl.extract_json('blah\n```json\n{"pattern": "x", "n": 2}\n```\ntail')
        self.assertEqual(got, {"pattern": "x", "n": 2})

    def test_bare_json_with_nested_braces_and_strings(self):
        got = drrtl.extract_json('lead {"a": {"b": 1}, "c": "} not the end"} trail')
        self.assertEqual(got, {"a": {"b": 1}, "c": "} not the end"})

    def test_no_json_returns_none(self):
        self.assertIsNone(drrtl.extract_json("no object here"))

    def test_single_verilog_block_for_single_file_design(self):
        reply = "```verilog\nmodule m(input a); endmodule\n```"
        got = drrtl.extract_verilog(reply, ["m.v"])
        self.assertEqual(list(got), ["m.v"])
        self.assertIn("module m", got["m.v"])

    def test_file_marker_names_the_target(self):
        reply = ("```verilog\n// FILE: top.v\nmodule top(); endmodule\n```\n"
                 "```verilog\n// FILE: sub.v\nmodule sub(); endmodule\n```")
        got = drrtl.extract_verilog(reply, ["top.v", "sub.v"])
        self.assertEqual(sorted(got), ["sub.v", "top.v"])
        self.assertNotIn("FILE:", got["top.v"])

    def test_multi_file_design_matched_by_module_name(self):
        reply = "```verilog\nmodule sub(); endmodule\n```"
        got = drrtl.extract_verilog(reply, ["top.v", "sub.v"])
        self.assertEqual(list(got), ["sub.v"])

    def test_json_block_is_not_mistaken_for_rtl(self):
        reply = '```json\n{"pattern": "p"}\n```\n```verilog\nmodule m(); endmodule\n```'
        self.assertEqual(list(drrtl.extract_verilog(reply, ["m.v"])), ["m.v"])

    def test_prose_only_reply_yields_nothing(self):
        self.assertEqual(drrtl.extract_verilog("I would pipeline it.", ["m.v"]), {})


# ===========================================================================
# tool dispatch
# ===========================================================================


class TestToolEnv(unittest.TestCase):
    def setUp(self):
        import toolenv
        self.te = toolenv
        self.addCleanup(os.environ.pop, "ASTRA_TOOL_PREFIX", None)

    def test_no_prefix_is_a_passthrough(self):
        os.environ.pop("ASTRA_TOOL_PREFIX", None)
        cmd, env = self.te.wrap(["yosys", "-c", "/a/b.tcl"], {"K": "/a/v"})
        self.assertEqual(cmd, ["yosys", "-c", "/a/b.tcl"])
        self.assertEqual(env, {"K": "/a/v"})
        self.assertFalse(self.te.dispatching())

    def test_prefix_rewrites_paths_and_passes_env(self):
        os.environ["ASTRA_TOOL_PREFIX"] = \
            f'docker run --rm -v "{self.te.ROOT}":/work -w /work astra:latest'
        cmd, env = self.te.wrap(
            ["yosys", "-c", f"{self.te.ROOT}/flow/scripts/synth.tcl"],
            {"ASTRA_OUT_DIR": f"{self.te.ROOT}/runs/x", "ASTRA_TOP": "m"})
        self.assertTrue(self.te.dispatching())
        # host paths gone from both argv and env
        self.assertNotIn(str(self.te.ROOT), " ".join(cmd[cmd.index("astra:latest"):]))
        self.assertEqual(env["ASTRA_OUT_DIR"], "/work/runs/x")
        self.assertEqual(env["ASTRA_ROOT"], "/work")
        # -e flags precede the image name, as docker requires
        self.assertLess(cmd.index("-e"), cmd.index("astra:latest"))
        self.assertEqual(cmd[-3:], ["yosys", "-c", "/work/flow/scripts/synth.tcl"])

    def test_quoted_mount_path_survives_shlex(self):
        os.environ["ASTRA_TOOL_PREFIX"] = \
            'docker run --rm -v "/a b/astra":/work astra:latest'
        cmd, _ = self.te.wrap(["yosys"], {})
        self.assertIn("/a b/astra:/work", cmd)


# ===========================================================================
# the skill learning agent's mechanical record
# ===========================================================================


class TestSkillLearning(unittest.TestCase):
    def test_records_every_labelled_candidate_with_its_advantage(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        lib = skills.SkillLibrary(Path(tmp.name) / "lib.json")
        agent = drrtl.SkillLearningAgent(lib, use_llm=False, model="opus",
                                         timeout=1)
        group = [
            {"id": "c0", "pattern": "serial accumulate chain",
             "strategy": "rebalance into an adder tree", "advantage": -1.0,
             "score": -0.4, "sec": {"equivalent": True}},
            {"id": "c1", "pattern": "wide comparison at end of datapath",
             "strategy": "drop the saturation entirely", "advantage": None,
             "score": None,
             "sec": {"equivalent": False, "method": "induction",
                     "reason": "counterexample found"}},
            {"id": "c2", "advantage": 0.5, "score": 0.1,
             "sec": {"equivalent": True}},          # unlabelled: skipped
        ]
        out = agent.learn(group, "mac_chain", 1)
        self.assertEqual(len(out["recorded"]), 2)
        self.assertEqual(len(lib), 2)

        by_pat = {e["pattern"]: e for e in lib.all(include_invalid=True)}
        good = by_pat["serial accumulate chain"]
        bad = by_pat["wide comparison at end of datapath"]
        self.assertEqual(good["stats"]["sec_pass"], 1)
        self.assertEqual(good["stats"]["advantage_n"], 1)
        self.assertEqual(bad["stats"]["sec_fail"], 1)
        self.assertEqual(bad["stats"]["advantage_n"], 0)
        self.assertGreater(good["confidence"], bad["confidence"])
        self.assertTrue(lib.path.is_file(), "the library must be persisted")


# ===========================================================================
# evaluation agent failure reporting
# ===========================================================================


class TestInconclusiveSec(unittest.TestCase):
    """A solver timeout is not evidence that a rewrite is wrong.

    Regression test for a real failure seen on the first live run: two
    retiming candidates hit the 1800s SEC timeout, and the skill learner
    recorded "moves a pipeline register -> breaks equivalence", which would
    have suppressed a legitimate strategy on every future run.
    """

    TIMEOUT = {"equivalent": False, "method": "error", "engine": "yosys",
               "reason": "yosys SEC timed out after 1800s"}
    REFUTED = {"equivalent": False, "method": "induction", "engine": "yosys",
               "reason": "counterexample found: the candidate is not equivalent"}
    PROVED = {"equivalent": True, "method": "induction", "engine": "yosys"}

    def test_sec_decided_separates_timeout_from_refutation(self):
        self.assertFalse(score.sec_decided({"sec": self.TIMEOUT}))
        self.assertTrue(score.sec_decided({"sec": self.REFUTED}))
        self.assertTrue(score.sec_decided({"sec": self.PROVED}))
        self.assertFalse(score.sec_decided({"sec": {"equivalent": False,
                                                    "method": "skipped"}}))

    def test_neither_can_be_promoted(self):
        """Eq. 4 is unchanged: undecided still means not promotable."""
        for verdict in (self.TIMEOUT, self.REFUTED):
            self.assertFalse(score.sec_passed({"sec": verdict}))

    def test_timeout_does_not_count_against_a_skill(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        lib = skills.SkillLibrary(Path(tmp.name) / "lib.json")
        for i in range(4):
            e = lib.record("unpipelined multiplier array", "retime registers",
                           sec_pass=False, conclusive=False, advantage=None,
                           design="mac_chain", iteration=i)
        self.assertEqual(e["stats"]["sec_fail"], 0)
        self.assertEqual(e["stats"]["occurrences"], 0)
        self.assertEqual(e["stats"]["inconclusive"], 4)
        self.assertEqual(e["stats"]["iterations"], 4)
        # Four undecided trials must not condemn the strategy.
        self.assertNotEqual(e["status"], "invalid")

    def test_a_real_refutation_still_condemns(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        lib = skills.SkillLibrary(Path(tmp.name) / "lib.json")
        for i in range(4):
            e = lib.record("drop a pipeline stage", "remove the register",
                           sec_pass=False, conclusive=True, advantage=None,
                           design="d", iteration=i)
        self.assertEqual(e["stats"]["sec_fail"], 4)
        self.assertEqual(e["status"], "invalid")

    def test_undecided_trials_do_not_dilute_a_good_record(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        lib = skills.SkillLibrary(Path(tmp.name) / "lib.json")
        for i in range(5):
            lib.record("serial accumulate chain", "rebalance into a tree",
                       sec_pass=True, conclusive=True, advantage=-1.0,
                       design="d", iteration=i)
        before = next(iter(lib.entries.values()))["confidence"]
        e = lib.record("serial accumulate chain", "rebalance into a tree",
                       sec_pass=False, conclusive=False, advantage=None,
                       design="d", iteration=5)
        self.assertEqual(e["confidence"], before)


class TestEvaluationFailures(unittest.TestCase):
    def test_a_failed_stage_produces_an_unscorable_candidate(self):
        m = drrtl.EvaluationAgent._failed(Path("/tmp/x"), "synth_failed",
                                          "yosys died", 1.5)
        self.assertIsNone(m["wns_ns"])
        self.assertIsNone(m["area_um2"])
        c = [{"id": "a", "metrics": m, "sec": {"equivalent": True}}]
        score.score_group(c, {"wns_ns": -0.5, "tns_ns": -1.0,
                              "area_um2": 100.0}, period_ns=2.0)
        self.assertIsNone(c[0]["score"])
        self.assertIsNone(score.select_best(c))


# ===========================================================================
# the directives that make the group a group
# ===========================================================================


class TestExploration(unittest.TestCase):
    def test_candidates_get_distinct_directives(self):
        seen = {drrtl._DIRECTIVES[i % len(drrtl._DIRECTIVES)] for i in range(4)}
        self.assertEqual(len(seen), 4)


# ===========================================================================
# path-portfolio selection
# ===========================================================================


def _stage(cell, pin, delay=0.02, fanout=None):
    return {"cell": cell, "pin": pin, "delay": delay, "time": 0.0,
            "fanout": fanout, "cap": None, "slew": None,
            "description": pin, "transition": None}


def _path(cone, end, slack, cells, delay=0.02, fanout=None, shared=0.8):
    """A synthetic OpenSTA path, in the shape parse_sta produces.

    Paths through one logic cone share most of their instances and diverge
    only near the endpoint, which is what a register bank actually produces --
    the reference run's twenty paths differ only in their last few stages.
    A fixture where they shared nothing would make clustering look broken when
    it is the fixture that is wrong.
    """
    k = int(len(cells) * shared)
    stages = [_stage("DFF_X1", f"{cone}_launch/Q", 0.03)]
    stages += [_stage(c, f"{cone}_n{i}/ZN", delay, fanout)
               for i, c in enumerate(cells[:k])]
    stages += [_stage(c, f"{end}_t{i}/ZN", delay, fanout)
               for i, c in enumerate(cells[k:])]
    stages.append(_stage("DFF_X1", f"{end}/D", 0.01))
    return {"startpoint": f"{cone}_launch/Q", "endpoint": f"{end}/Q",
            "slack_ns": slack, "status": "VIOLATED" if slack < 0 else "MET",
            "stages": stages, "logic_depth": len(cells),
            "arrival_ns": None, "required_ns": None,
            "path_group": "clk", "path_type": "max"}


def _empty_index():
    return {"cells": {}, "nets": {}, "modules": [], "available": False}


class _NoRtl(rtl_map.RtlIndex):
    def __init__(self):
        super().__init__([])


class TestPathSelMetrics(unittest.TestCase):
    def test_jaccard(self):
        self.assertEqual(pathsel.jaccard({"a", "b"}, {"a", "b"}), 1.0)
        self.assertEqual(pathsel.jaccard({"a"}, {"b"}), 0.0)
        self.assertEqual(pathsel.jaccard(set(), set()), 0.0,
                         "two empty sets are unknown, not identical")
        self.assertAlmostEqual(pathsel.jaccard({"a", "b"}, {"b", "c"}), 1 / 3)

    def test_weighted_jaccard_reduces_to_jaccard_on_indicators(self):
        a, b = {"x": 1.0, "y": 1.0}, {"y": 1.0, "z": 1.0}
        self.assertAlmostEqual(pathsel.weighted_jaccard(a, b),
                               pathsel.jaccard(set(a), set(b)))

    def test_weighted_jaccard_separates_unequal_weights(self):
        """Two paths both touching one line are not the same path if one
        spends 90% of its delay there and the other 10%."""
        heavy, light = {"L": 0.9, "M": 0.1}, {"L": 0.1, "M": 0.9}
        self.assertLess(pathsel.weighted_jaccard(heavy, light), 0.5)

    def test_hist_similarity_bounds(self):
        h = {"xor": 0.5, "adder": 0.5}
        self.assertAlmostEqual(pathsel.hist_similarity(h, h), 1.0)
        self.assertAlmostEqual(
            pathsel.hist_similarity({"xor": 1.0}, {"mux": 1.0}), 0.0)

    def test_similarity_is_reflexive_symmetric_and_bounded(self):
        nl, rtl = _empty_index(), _NoRtl()
        fa = pathsel.path_features(_path("a", "z", -0.3, ["XOR2_X1"] * 6), 0,
                                   nl, rtl, 2.0)
        fb = pathsel.path_features(_path("b", "y", -0.1, ["MUX2_X1"] * 6), 1,
                                   nl, rtl, 2.0)
        self.assertAlmostEqual(pathsel.similarity(fa, fa), 1.0, places=6)
        self.assertAlmostEqual(pathsel.similarity(fa, fb),
                               pathsel.similarity(fb, fa))
        for x in (pathsel.similarity(fa, fb), pathsel.similarity(fa, fa)):
            self.assertGreaterEqual(x, 0.0)
            self.assertLessEqual(x, 1.0)


class TestPathSelClustering(unittest.TestCase):
    """A register bank produces many paths and one bottleneck."""

    def _bank(self, n=20):
        return [_path("launch", f"acc_out[{i}]", -0.30 + 0.01 * i,
                      ["XOR2_X1", "AOI21_X1"] * 8) for i in range(n)]

    def test_one_cone_collapses_to_one_cluster(self):
        nl, rtl = _empty_index(), _NoRtl()
        feats = [pathsel.path_features(p, i, nl, rtl, 2.0)
                 for i, p in enumerate(self._bank())]
        clusters, _ = pathsel.cluster(feats)
        self.assertEqual(len(clusters), 1,
                         "20 bit-slices of one cone are one bottleneck")

    def test_two_independent_cones_stay_apart(self):
        nl, rtl = _empty_index(), _NoRtl()
        paths = ([_path("mac", f"acc[{i}]", -0.3, ["XOR2_X1", "AOI21_X1"] * 8)
                  for i in range(6)]
                 + [_path("sel", f"out[{i}]", -0.2, ["MUX2_X1", "AND2_X1"] * 8)
                    for i in range(6)])
        feats = [pathsel.path_features(p, i, nl, rtl, 2.0)
                 for i, p in enumerate(paths)]
        clusters, _ = pathsel.cluster(feats)
        self.assertEqual(len(clusters), 2)
        self.assertEqual(sorted(len(c) for c in clusters), [6, 6])

    def test_clustering_is_order_independent(self):
        import random
        nl, rtl = _empty_index(), _NoRtl()
        paths = self._bank(6) + [
            _path("sel", f"o[{i}]", -0.1, ["MUX2_X1"] * 10) for i in range(4)]
        partitions = set()
        for seed in range(4):
            shuffled = list(paths)
            random.Random(seed).shuffle(shuffled)
            feats = [pathsel.path_features(p, i, nl, rtl, 2.0)
                     for i, p in enumerate(shuffled)]
            clusters, _ = pathsel.cluster(feats)
            partitions.add(frozenset(
                frozenset(shuffled[i]["endpoint"] for i in c) for c in clusters))
        self.assertEqual(len(partitions), 1,
                         "the same paths must partition the same way")

    def test_raising_the_threshold_never_merges_more(self):
        nl, rtl = _empty_index(), _NoRtl()
        paths = self._bank(4) + [
            _path("sel", f"o[{i}]", -0.1, ["MUX2_X1"] * 10) for i in range(4)]
        feats = [pathsel.path_features(p, i, nl, rtl, 2.0)
                 for i, p in enumerate(paths)]
        counts = [len(pathsel.cluster(feats, t)[0])
                  for t in (0.2, 0.4, 0.6, 0.8, 0.95)]
        self.assertEqual(counts, sorted(counts),
                         "a stricter threshold cannot produce fewer clusters")


class TestPathSelValue(unittest.TestCase):
    def _feats(self, paths):
        nl, rtl = _empty_index(), _NoRtl()
        return [pathsel.path_features(p, i, nl, rtl, 2.0)
                for i, p in enumerate(paths)]

    def test_severity_is_strictly_monotone_across_the_whole_range(self):
        """Both halves matter. A met design must be rankable (the old gate
        zeroed it), and so must a violating one (clipping at the constraint
        would flatten it)."""
        sev = [pathsel.severity(sl, 2.0)
               for sl in (2.0, 0.9, 0.5, 0.1, 0.0, -0.1, -0.5, -2.0)]
        self.assertTrue(all(x < y for x, y in zip(sev, sev[1:])),
                        f"not strictly increasing as slack falls: {sev}")
        self.assertEqual(sev[0], 0.0, "a full period of headroom")
        self.assertEqual(sev[-1], 1.0, "a full period over the constraint")

    def test_severity_puts_the_constraint_at_the_midpoint(self):
        self.assertAlmostEqual(pathsel.severity(0.0, 2.0), 0.5)

    def test_a_passing_path_still_has_a_severity(self):
        """alu32's real numbers: WNS +0.3701 at a 2.5 ns period. Under the old
        gate this scored 0.0 and was indistinguishable from a path with a full
        period of headroom."""
        sev = pathsel.severity(0.3701, 2.5)
        self.assertGreater(sev, 0.0)
        self.assertLess(sev, 0.5, "it meets timing, so it sits below the "
                                  "constraint midpoint")
        self.assertGreater(sev, pathsel.severity(1.5, 2.5),
                           "and above a path with far more headroom")

    def test_severity_saturates_at_one_period(self):
        self.assertEqual(pathsel.severity(-9.0, 2.0), 1.0)
        self.assertEqual(pathsel.severity(2.0, 2.0), 0.0)

    def test_tns_shares_sum_to_one_when_violating(self):
        feats = self._feats([_path("a", "x", -0.4, ["XOR2_X1"] * 5),
                             _path("b", "y", -0.2, ["MUX2_X1"] * 5),
                             _path("c", "z", -0.1, ["AND2_X1"] * 5)])
        clusters, _ = pathsel.cluster(feats, 0.99)
        total = sum(pathsel.cluster_value([feats[i] for i in c], feats, 2.0)
                    ["tns_share"] for c in clusters)
        self.assertAlmostEqual(total, 1.0)

    def test_a_fully_passing_design_does_not_divide_by_zero(self):
        feats = self._feats([_path("a", "x", 0.4, ["XOR2_X1"] * 5),
                             _path("b", "y", 0.9, ["MUX2_X1"] * 5)])
        for c in ([feats[0]], [feats[1]]):
            v = pathsel.cluster_value(c, feats, 2.0)
            self.assertTrue(0.0 <= v["terms"]["impact"] <= 1.0)
            self.assertTrue(0.0 <= v["terms"]["severity"] <= 1.0)

    def test_impact_uses_the_probability_when_one_is_given(self):
        feats = self._feats([_path("a", "x", -0.4, ["XOR2_X1"] * 5)])
        v = pathsel.cluster_value(feats, feats, 2.0, p_crit=0.42)
        self.assertAlmostEqual(v["terms"]["impact"], 0.42)
        self.assertAlmostEqual(v["p_critical"], 0.42)


class TestPathSelCriticality(unittest.TestCase):
    """P(this path is the one limiting the clock).

    Static timing analysis is deterministic, so this is NOT a probability that
    the design fails and NOT statistical STA over process variation. It says
    how much the post-synthesis ordering of near-tied paths can be trusted
    before layout has put any real wire delay in the report.
    """

    def _feats(self, paths):
        nl, rtl = _empty_index(), _NoRtl()
        return [pathsel.path_features(p, i, nl, rtl, 2.0)
                for i, p in enumerate(paths)]

    def _two_cones(self, sa, sb):
        return self._feats(
            [_path("a", f"x[{i}]", sa, ["XOR2_X1", "AOI21_X1"] * 5)
             for i in range(3)]
            + [_path("b", f"y[{i}]", sb, ["MUX2_X1", "AND2_X1"] * 5)
               for i in range(3)])

    def test_probabilities_sum_to_one(self):
        feats = self._two_cones(-0.30, -0.28)
        clusters, _ = pathsel.cluster(feats)
        per_path, per_cone = pathsel.criticality(feats, clusters, 2.0)
        self.assertAlmostEqual(sum(per_path), 1.0, places=6)
        self.assertAlmostEqual(sum(per_cone), 1.0, places=6)

    def test_zero_sigma_is_the_deterministic_answer(self):
        feats = self._two_cones(-0.30, -0.10)
        clusters, _ = pathsel.cluster(feats)
        _, per_cone = pathsel.criticality(feats, clusters, 2.0, sigma=0.0)
        self.assertAlmostEqual(max(per_cone), 1.0)
        self.assertAlmostEqual(min(per_cone), 0.0)

    def test_more_uncertainty_spreads_the_distribution(self):
        feats = self._two_cones(-0.30, -0.28)
        clusters, _ = pathsel.cluster(feats)
        spreads = []
        for sigma in (0.001, 0.02, 0.20):
            _, per_cone = pathsel.criticality(feats, clusters, 2.0, sigma=sigma)
            spreads.append(max(per_cone) - min(per_cone))
        self.assertEqual(spreads, sorted(spreads, reverse=True),
                         "more delay uncertainty must mean less certainty "
                         "about which cone is the limiter")

    def test_a_clearly_worse_cone_keeps_the_weight(self):
        feats = self._two_cones(-0.90, -0.05)
        clusters, _ = pathsel.cluster(feats)
        _, per_cone = pathsel.criticality(feats, clusters, 2.0)
        self.assertGreater(max(per_cone), 0.98,
                           "0.85 ns apart is far outside the noise")

    def test_a_cone_holds_its_weight_against_its_own_bit_slices(self):
        """Without the shared-cone term, twenty bit-slices of one bottleneck
        would each take 1/20 of the criticality and the cone that owns all of
        it would look unimportant."""
        feats = self._feats(
            [_path("bank", f"acc[{i}]", -0.30, ["XOR2_X1", "AOI21_X1"] * 8)
             for i in range(20)]
            + [_path("other", "z[0]", -0.29, ["MUX2_X1", "AND2_X1"] * 8)])
        clusters, _ = pathsel.cluster(feats)
        self.assertEqual(len(clusters), 2)
        per_path, per_cone = pathsel.criticality(feats, clusters, 2.0)
        big = max(range(2), key=lambda c: len(clusters[c]))
        self.assertGreater(per_cone[big], 0.4,
                           "the cone must not be diluted by its own members")
        self.assertLess(max(per_path[i] for i in clusters[big]), per_cone[big],
                        "no single bit-slice owns the cone's whole weight")

    def test_it_works_with_no_violation_at_all(self):
        """The case that motivates the whole distribution: minimising delay
        against a constraint the design already meets."""
        feats = self._two_cones(0.05, 0.30)
        clusters, _ = pathsel.cluster(feats)
        _, per_cone = pathsel.criticality(feats, clusters, 2.0)
        self.assertAlmostEqual(sum(per_cone), 1.0, places=6)
        tight = min(range(len(clusters)),
                    key=lambda c: min(feats[i].slack_ns for i in clusters[c]))
        self.assertGreater(per_cone[tight], 0.9,
                           "the path with the least headroom still limits the "
                           "clock, violation or not")

    def test_it_is_reproducible(self):
        feats = self._two_cones(-0.30, -0.29)
        clusters, _ = pathsel.cluster(feats)
        a = pathsel.criticality(feats, clusters, 2.0)
        b = pathsel.criticality(feats, clusters, 2.0)
        self.assertEqual(a, b, "a fixed seed, like the rest of the pipeline")

    def test_a_path_with_no_slack_reported_cannot_win(self):
        paths = [_path("a", "x", -0.3, ["XOR2_X1"] * 6),
                 _path("b", "y", -0.2, ["MUX2_X1"] * 6)]
        paths[1]["slack_ns"] = None
        feats = self._feats(paths)
        clusters, _ = pathsel.cluster(feats)
        per_path, _ = pathsel.criticality(feats, clusters, 2.0)
        self.assertGreater(per_path[0], 0.99)
        self.assertLess(per_path[1], 0.01)

    def test_a_met_design_ranks_cones_that_the_old_gate_could_not(self):
        """Regression guard on the bug this replaced: criticality used to be
        hard-zeroed for any path with positive slack, so every target in a
        design that met timing scored identically and could not be ordered.

        Both cones meet timing, and they are close enough that neither is
        dismissed -- which is the situation where the ranking has to work."""
        nl, rtl = _empty_index(), _NoRtl()
        paths = ([_path("tight", f"x[{i}]", 0.05, ["XOR2_X1", "AOI21_X1"] * 8)
                  for i in range(4)]
                 + [_path("loose", f"y[{i}]", 0.12, ["MUX2_X1", "AND2_X1"] * 8)
                    for i in range(4)])
        sel = pathsel.build_targets(
            {"summary": {"wns_ns": 0.05, "tns_ns": 0.0}, "critical_paths": paths},
            nl, rtl, 2.0, k=2)
        self.assertEqual(sel["cluster_count"], 2)
        cones = {t.id: t for t in sel["targets"] if t.kind == "cone"}
        self.assertEqual(len(cones), 2, "both cones must clear the floor")
        vals = [t.value for t in sel["targets"]]
        self.assertGreater(vals[0], vals[1],
                           "the cone with less headroom must rank first")
        self.assertGreater(sel["targets"][0].p_critical,
                           sel["targets"][1].p_critical)

    def test_a_dominated_cone_falls_below_the_floor(self):
        """The other side of it: a cone that is nowhere near limiting the
        clock is not worth an agent, and k drops rather than padding."""
        nl, rtl = _empty_index(), _NoRtl()
        paths = ([_path("tight", f"x[{i}]", 0.05, ["XOR2_X1", "AOI21_X1"] * 8)
                  for i in range(4)]
                 + [_path("loose", f"y[{i}]", 0.90, ["MUX2_X1", "AND2_X1"] * 8)
                    for i in range(4)])
        sel = pathsel.build_targets(
            {"summary": {"wns_ns": 0.05, "tns_ns": 0.0}, "critical_paths": paths},
            nl, rtl, 2.0, k=2)
        self.assertEqual(sel["cluster_count"], 2)
        self.assertTrue(sel["collapsed"],
                        "one viable cone means segments, not a padded second "
                        "cone that cannot limit the clock")


class TestPathSelMMR(unittest.TestCase):
    def _setup(self, paths):
        nl, rtl = _empty_index(), _NoRtl()
        feats = [pathsel.path_features(p, i, nl, rtl, 2.0)
                 for i, p in enumerate(paths)]
        clusters, sim = pathsel.cluster(feats, 0.99)
        return feats, clusters, sim

    def test_lambda_zero_is_pure_value_order(self):
        feats, clusters, sim = self._setup([
            _path("a", "x", -0.5, ["XOR2_X1"] * 5),
            _path("b", "y", -0.2, ["MUX2_X1"] * 5),
            _path("c", "z", -0.1, ["AND2_X1"] * 5)])
        picked = pathsel.select_clusters(clusters, feats, sim, 2.0, 3, lam=0.0)
        vals = [p["value"] for p in picked]
        self.assertEqual(vals, sorted(vals, reverse=True))

    def test_k_is_a_ceiling_not_a_quota(self):
        """One cone must yield one cone-target, never three padded ones."""
        feats, clusters, sim = self._setup([
            _path("launch", f"acc[{i}]", -0.3, ["XOR2_X1", "AOI21_X1"] * 8)
            for i in range(12)])
        picked = pathsel.select_clusters(clusters, feats, sim, 2.0, k=3)
        self.assertEqual(len(picked), 1)

    def test_a_negligible_cluster_is_not_a_target(self):
        feats, clusters, sim = self._setup([
            _path("a", "x", -10.0, ["XOR2_X1"] * 5),
            _path("b", "y", -0.0001, ["MUX2_X1"] * 5)])
        picked = pathsel.select_clusters(clusters, feats, sim, 2.0, k=3)
        self.assertEqual(len(picked), 1,
                         "a cluster under the mass floor is not worth an agent")


class TestPathSelSegments(unittest.TestCase):
    """When a design has one cone, its path is cut into disjoint spans."""

    def _stages(self):
        # Three visibly different regions: an AND array, a carry region, a
        # final reduction -- the shape a MAC actually synthesises to.
        return ([_stage("AND2_X1", f"a{i}/ZN", 0.03) for i in range(12)]
                + [_stage("XOR2_X1", f"x{i}/ZN", 0.03) for i in range(12)]
                + [_stage("OAI21_X1", f"o{i}/ZN", 0.03) for i in range(12)])

    def test_spans_are_disjoint_and_cover_the_path(self):
        spans = pathsel.split_points(self._stages(), 3)
        self.assertEqual(len(spans), 3)
        self.assertEqual(spans[0][0], 0)
        self.assertEqual(spans[-1][1], 36)
        for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
            self.assertEqual(a1, b0, "spans must abut, not overlap or gap")

    def test_cuts_land_on_the_cell_mix_boundaries(self):
        """Within a cell of the transition. The objective trades purity against
        which span is worth cutting, so it can settle a cell either side of a
        boundary; what matters is that each segment is dominated by the family
        it was cut for."""
        spans = pathsel.split_points(self._stages(), 3)
        starts = [sp[0] for sp in spans]
        self.assertEqual(starts[0], 0)
        self.assertLessEqual(abs(starts[1] - 12), 1)
        self.assertLessEqual(abs(starts[2] - 24), 1)
        stages = self._stages()
        for span, want in zip(spans, ("and_or", "xor", "aoi_oai")):
            fam = pathsel._span_families(stages[span[0]:span[1]])
            self.assertEqual(max(fam, key=fam.get), want)

    def test_delay_shares_sum_to_one(self):
        stages = self._stages()
        spans = pathsel.split_points(stages, 3)
        total = sum(float(s["delay"]) for s in stages)
        shares = [sum(float(s["delay"]) for s in stages[a:b]) / total
                  for a, b in spans]
        self.assertAlmostEqual(sum(shares), 1.0)

    def test_a_short_path_is_not_split(self):
        short = [_stage("AND2_X1", f"a{i}/ZN", 0.03) for i in range(5)]
        self.assertEqual(pathsel.split_points(short, 3), [(0, 5)],
                         "a five-cell path has no three attackable segments")

    def test_k_of_one_never_splits(self):
        self.assertEqual(pathsel.split_points(self._stages(), 1), [(0, 36)])


class TestPathSelOnRealRun(unittest.TestCase):
    """The honesty guard, pinned to a committed artifact.

    If a weight tweak ever makes the selector report three mutually similar
    targets on a design that genuinely has one bottleneck, this fails.
    """

    RUN = ROOT / "runs" / "mac_chain" / "opt-20260901-025558-run1" / "baseline"

    def setUp(self):
        if not (self.RUN / "02_sta" / "timing.json").is_file():
            self.skipTest("reference run artifact not present")

    def test_the_reference_design_is_one_cone(self):
        sel = pathsel.from_run(self.RUN, k=3)
        self.assertEqual(sel["cluster_count"], 1,
                         "20 reported paths there are one bottleneck cone")
        self.assertTrue(sel["collapsed"],
                        "with one cone the portfolio must fall back to segments")

    def test_segments_are_distinct_and_ordered(self):
        sel = pathsel.from_run(self.RUN, k=3)
        targets = sel["targets"]
        self.assertEqual(len(targets), 3)
        self.assertTrue(all(t.kind == "segment" for t in targets))
        spans = [t.stage_span for t in targets]
        for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
            self.assertEqual(a1, b0)
        self.assertAlmostEqual(sum(t.delay_share for t in targets), 1.0, places=2)

    def test_segments_recover_the_declared_bottlenecks(self):
        """The design declares three bottlenecks by hand in config.json.
        The selector never reads that field; it should find them anyway."""
        sel = pathsel.from_run(self.RUN, k=3)
        fams = [t.cell_families for t in sel["targets"]]
        self.assertEqual(max(fams[0], key=fams[0].get), "and_or",
                         "the first segment is the partial-product AND array")
        self.assertIn("xor", fams[1], "the middle segment is carry propagation")
        self.assertEqual(max(fams[2], key=fams[2].get), "aoi_oai",
                         "the last segment is the saturation reduction")
        self.assertGreaterEqual(sel["targets"][0].max_fanout, 8,
                                "the multiplicand broadcast is a real hotspot")


# ===========================================================================
# mechanical union
# ===========================================================================


class TestMerge(unittest.TestCase):
    PARENT = {"m.v": "module m;\nwire a = x + y;\nwire b = p * q;\n"
                     "wire c = a | b;\nendmodule\n"}

    def _cand(self, old, new):
        return {"m.v": self.PARENT["m.v"].replace(old, new)}

    def test_disjoint_edits_both_apply(self):
        merged, rep = merge.mechanical_union(self.PARENT, {
            "t1": self._cand("x + y", "(x + y) >> 1"),
            "t2": self._cand("a | b", "a ^ b")})
        self.assertIn("(x + y) >> 1", merged["m.v"])
        self.assertIn("a ^ b", merged["m.v"])
        self.assertEqual(rep["conflicts"], [])

    def test_overlapping_edits_are_dropped_not_arbitrated(self):
        merged, rep = merge.mechanical_union(self.PARENT, {
            "t1": self._cand("x + y", "(x + y) >> 1"),
            "t2": self._cand("x + y", "x - y")})
        self.assertIn("wire a = x + y;", merged["m.v"],
                      "a contested region keeps the parent's text")
        self.assertEqual(len(rep["conflicts"]), 1)
        self.assertEqual(rep["conflicts"][0]["candidates"], ["t1", "t2"])

    def test_union_is_order_independent(self):
        import itertools
        cands = {"t1": self._cand("x + y", "(x + y) >> 1"),
                 "t2": self._cand("a | b", "a ^ b"),
                 "t3": self._cand("x + y", "x - y")}
        results = {merge.mechanical_union(self.PARENT, dict(perm))[0]["m.v"]
                   for perm in itertools.permutations(cands.items())}
        self.assertEqual(len(results), 1,
                         "a control whose result depends on input order is "
                         "not a control")

    def test_a_reformatted_candidate_is_excluded_with_a_reason(self):
        reflow = {"m.v": "\n".join("    " + ln for ln
                                   in self.PARENT["m.v"].splitlines()) + "\n"}
        _, rep = merge.mechanical_union(self.PARENT, {
            "t1": self._cand("a | b", "a ^ b"), "t2": reflow})
        self.assertIn("t2", rep["excluded"])
        self.assertEqual(rep["applied_candidates"], ["t1"],
                         "one candidate reformatting must not poison the rest")

    def test_an_unchanged_candidate_contributes_nothing(self):
        merged, rep = merge.mechanical_union(self.PARENT, {"t1": self.PARENT})
        self.assertFalse(rep["changed"])
        self.assertEqual(merge.normalise(merged["m.v"]),
                         merge.normalise(self.PARENT["m.v"]))

    def test_trailing_whitespace_is_not_a_hunk(self):
        """A model that re-emits the file with trailing spaces has not edited
        it, and must not be treated as having rewritten every line."""
        padded = {"m.v": self.PARENT["m.v"].replace(";", ";   ")
                                           .replace("\n\n", "\n\n\n")}
        p = merge.normalise(self.PARENT["m.v"])
        c = merge.normalise(padded["m.v"])
        self.assertEqual(merge.hunks(p, c, "t1", "m.v"), [])

    def test_subsets_are_every_combination_of_two_or_more(self):
        got = merge.subsets(["t1", "t2", "t3"])
        self.assertEqual(len(got), 4)
        self.assertEqual(got[0], ("t1", "t2", "t3"))
        self.assertEqual({merge.union_id(s) for s in got},
                         {"u123", "u12", "u13", "u23"})

    def test_insertions_claim_the_joint_they_sit_in(self):
        """Two agents inserting at the same point conflict, even though
        neither replaced any parent line."""
        a = {"m.v": self.PARENT["m.v"].replace(
            "wire b", "wire n1 = x & y;\nwire b")}
        b = {"m.v": self.PARENT["m.v"].replace(
            "wire b", "wire n2 = x ^ y;\nwire b")}
        _, rep = merge.mechanical_union(self.PARENT, {"t1": a, "t2": b})
        self.assertEqual(len(rep["conflicts"]), 1)


# ===========================================================================
# pre-synthesis scan
# ===========================================================================


class TestRtlScan(unittest.TestCase):
    CHAIN = """
    module t(input clk, input signed [15:0] a, b, c, d,
             output reg signed [39:0] o);
      wire signed [39:0] s0 = a;
      wire signed [39:0] s1 = s0 + b;
      wire signed [39:0] s2 = s1 + c;
      wire signed [39:0] s3 = s2 + d;
      always @(posedge clk) o <= s3;
    endmodule
    """
    TREE = """
    module t(input clk, input signed [15:0] a, b, c, d,
             output reg signed [39:0] o);
      wire signed [39:0] l = a + b;
      wire signed [39:0] r = c + d;
      wire signed [39:0] s = l + r;
      always @(posedge clk) o <= s;
    endmodule
    """

    def _scan(self, src):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "t.v"
            f.write_text(src)
            return rtlscan.scan([f])

    def _patterns(self, src):
        return {f["pattern"] for f in self._scan(src)["findings"]}

    def test_finds_a_serial_reduction_chain(self):
        self.assertIn("serial reduction chain written as a chain, not a tree",
                      self._patterns(self.CHAIN))

    def test_a_balanced_tree_is_the_negative_control(self):
        self.assertNotIn("serial reduction chain written as a chain, not a tree",
                         self._patterns(self.TREE),
                         "a tree must not be reported as a chain")

    def test_chain_finding_carries_the_depth_it_claims(self):
        f = next(x for x in self._scan(self.CHAIN)["findings"]
                 if x["pattern"].startswith("serial reduction"))
        self.assertEqual(f["metric"]["length"], 3)
        self.assertEqual(f["metric"]["tree_depth"], 2)
        self.assertEqual(f["metric"]["op"], "+")

    def test_width_expressions_are_not_arithmetic(self):
        """`{{(ACC-2*W){p[31]}}, p}` is a sign extension, not a multiply."""
        src = """
        module t(input clk, input signed [31:0] p,
                 output reg signed [39:0] o);
          parameter integer W = 16;
          parameter integer ACC = 40;
          wire signed [39:0] e = {{(ACC-2*W){p[2*W-1]}}, p};
          always @(posedge clk) o <= e;
        endmodule
        """
        self.assertNotIn("unpipelined multiply in one cycle",
                         self._patterns(src))

    def test_finds_a_generate_elaborated_chain(self):
        src = """
        module t(input clk, input [31:0] r, output reg o);
          parameter integer N = 32;
          wire [N:0] h;
          assign h[N] = 1'b0;
          genvar i;
          generate
            for (i = N-1; i >= 0; i = i - 1) begin : g
              assign h[i] = r[i] | h[i+1];
            end
          endgenerate
          always @(posedge clk) o <= h[0];
        endmodule
        """
        f = next(x for x in self._scan(src)["findings"]
                 if x["pattern"] == "serial chain elaborated by a generate loop")
        self.assertEqual(f["metric"]["trips"], 32)
        self.assertEqual(f["metric"]["signal"], "h")

    def test_statements_span_lines(self):
        src = """
        module t(input clk, input [7:0] a, b, c, d, e,
                 output reg [7:0] o);
          wire [7:0] w = a ? b :
                         c ? d :
                         b ? c :
                         d ? e : a;
          always @(posedge clk) o <= w;
        endmodule
        """
        self.assertIn("cascaded conditional select", self._patterns(src))

    def test_shipped_designs_behave_as_documented(self):
        """mac_chain declares three bottlenecks; alu32 declares none."""
        for name, expect in (("mac_chain", True), ("alu32", False)):
            rtl = ROOT / "designs" / name / "rtl" / f"{name}.v"
            if not rtl.is_file():
                self.skipTest(f"{name} not present")
            high = [f for f in rtlscan.scan([rtl])["findings"]
                    if f["severity"] == "high"]
            self.assertEqual(bool(high), expect, f"{name}: {high}")


class TestRtlScanDepth(unittest.TestCase):
    def test_adder_depth_grows_with_width(self):
        d = [rtlscan.expr_depth("a + b", w)[0] for w in (4, 16, 64)]
        self.assertEqual(d, sorted(d))
        self.assertTrue(all(x < y for x, y in zip(d, d[1:])))

    def test_a_constant_shift_is_wiring_a_variable_shift_is_not(self):
        const, _ = rtlscan.expr_depth("a >> 3", 32)
        var, _ = rtlscan.expr_depth("a >> b", 32)
        self.assertLess(const, var)

    def test_an_unparsable_expression_returns_zero_and_does_not_raise(self):
        self.assertEqual(rtlscan.expr_depth("", 8), (0, []))
        self.assertEqual(rtlscan.expr_depth("plain_signal", 8), (0, []))


# ===========================================================================
# the skill document
# ===========================================================================


class TestSkillDoc(unittest.TestCase):
    def setUp(self):
        if not skillgen.SKILL_PATH.is_file():
            self.skipTest("no SKILL.md -- run: astra skilldoc build")
        self.raw = skillgen.SKILL_PATH.read_text()

    def test_has_frontmatter_with_a_name_and_description(self):
        self.assertTrue(self.raw.startswith("---"))
        head = self.raw.split("---", 2)[1]
        self.assertIn("name: rtl-timing-optimization", head)
        self.assertIn("description:", head)

    def test_makes_no_numeric_evidence_claims(self):
        """Statistics live in the library, where they are measured. A skill
        document asserting a success rate would be indistinguishable from a
        measured one."""
        import re as _re
        body = skillgen.load_doc()
        for pat in (r"\d+\s*%\s*(?:of|success|pass)", r"\bn\s*=\s*\d+",
                    r"confidence\s+[0-9.]+", r"\d+\s*/\s*\d+\s*trials"):
            self.assertIsNone(_re.search(pat, body, _re.I),
                              f"skill document asserts evidence: {pat}")

    def test_frontmatter_is_stripped_before_injection(self):
        doc = skillgen.load_doc()
        self.assertFalse(doc.startswith("---"))
        self.assertTrue(doc)

    def test_injection_truncates_at_a_line_and_says_so(self):
        out = skillgen.inject("SYS.", skillgen.load_doc(), budget=400)
        self.assertIn("[skill document truncated", out)
        self.assertLess(len(out), 700)

    def test_a_missing_document_is_a_no_op_not_a_failure(self):
        self.assertEqual(skillgen.inject("SYS.", ""), "SYS.")
        self.assertEqual(skillgen.load_doc(Path("/nonexistent/SKILL.md")), "")

    def test_seed_entries_carry_no_statistics(self):
        """A seed is an untested suggestion and must stay one until a run
        gives it a record. Learned entries are exempt -- carrying measured
        statistics is the whole point of them."""
        lib = skills.SkillLibrary()
        for e in lib.all(True):
            if e.get("source") != "seed":
                continue
            st = e.get("stats") or {}
            self.assertEqual(st.get("occurrences", 0), 0,
                             f"{e['id']} is a seed with statistics")
            self.assertEqual(e.get("confidence", 0.0), 0.0)

    def test_the_document_states_no_record_for_any_entry(self):
        """The catalogue is an index. Statistics belong in the library, where
        they were measured, and the per-target skill block is what carries
        them into a prompt."""
        for line in self.raw.splitlines():
            if line.startswith("| ") and line.count("|") == 3:
                self.assertNotIn("confidence", line.lower())
                self.assertNotIn("SEC pass", line)


# ===========================================================================
# stage parameterisation (post-PnR readiness, without OpenROAD)
# ===========================================================================


class TestStageParameterisation(unittest.TestCase):
    """Proves the pnr path reads the right file, with no 20 GB image."""

    def _run_dir(self, root: Path) -> Path:
        r = root / "run"
        for sub, tag, endpoint in (("02_sta", "post_synth", "synth_ep"),
                                   ("03_pnr", "post_gr", "pnr_ep")):
            (r / sub).mkdir(parents=True, exist_ok=True)
            (r / sub / "timing.json").write_text(json.dumps({
                "tag": tag, "summary": {"wns_ns": -0.5, "tns_ns": -1.0},
                "critical_paths": [_path("launch", endpoint, -0.5,
                                         ["XOR2_X1"] * 6)]}))
        (r / "00_inputs").mkdir(parents=True, exist_ok=True)
        (r / "01_synth").mkdir(parents=True, exist_ok=True)
        return r

    def test_each_stage_reads_its_own_report(self):
        with tempfile.TemporaryDirectory() as d:
            r = self._run_dir(Path(d))
            sta = rtl_map.from_run(r, period_ns=2.0, stage="sta")
            pnr = rtl_map.from_run(r, period_ns=2.0, stage="pnr")
            self.assertIn("synth_ep", sta["paths"][0]["endpoint"])
            self.assertIn("pnr_ep", pnr["paths"][0]["endpoint"])

    def test_the_default_is_post_synthesis(self):
        with tempfile.TemporaryDirectory() as d:
            r = self._run_dir(Path(d))
            self.assertIn("synth_ep",
                          rtl_map.from_run(r, period_ns=2.0)["paths"][0]["endpoint"])

    def test_an_unknown_stage_raises(self):
        with self.assertRaises(ValueError):
            rtl_map.from_run(Path("/tmp"), stage="floorplan")

    def test_pathsel_reports_a_missing_report_clearly(self):
        with tempfile.TemporaryDirectory() as d:
            r = Path(d) / "run"
            (r / "02_sta").mkdir(parents=True)
            (r / "02_sta" / "timing.json").write_text(
                json.dumps({"critical_paths": []}))
            with self.assertRaises(FileNotFoundError) as cm:
                pathsel.from_run(r, stage="pnr")
            self.assertIn("--pnr", str(cm.exception))


# ===========================================================================
# the portfolio orchestrator's arithmetic
# ===========================================================================


class TestPortfolioBudget(unittest.TestCase):
    def _args(self, *argv):
        return portfolio.build_parser().parse_args(["mac_chain", *argv])

    def test_the_documented_formula(self):
        a = self._args()
        self.assertEqual(portfolio.call_budget(a),
                         a.clean + a.iters * (a.top_k + 2))

    def test_each_switch_removes_one_call_per_iteration(self):
        base = portfolio.call_budget(self._args())
        iters = self._args().iters
        self.assertEqual(portfolio.call_budget(self._args("--no-merge-agent")),
                         base - iters)
        self.assertEqual(portfolio.call_budget(self._args("--no-skill-agent")),
                         base - iters)

    def test_a_dry_run_costs_nothing(self):
        self.assertEqual(portfolio.call_budget(self._args("--dry-run")), 0)

    def test_the_model_is_selectable_for_a_head_to_head(self):
        """The addon must be runnable on the same model as the base loop,
        or a comparison between them measures the model, not the method."""
        self.assertEqual(self._args("--model", "haiku").model, "haiku")
        self.assertEqual(
            drrtl.build_parser().parse_args(["mac_chain", "--model", "haiku"]).model,
            "haiku")

    def test_the_pool_default_is_widened(self):
        """20 paths is one per endpoint, which truncates the TNS denominator
        on anything bigger than the shipped designs."""
        self.assertGreater(self._args().npaths,
                           drrtl.build_parser().parse_args(["mac_chain"]).npaths)

    def test_portfolio_runs_write_to_their_own_prefix(self):
        self.assertNotEqual(portfolio.PortfolioOrchestrator.RUN_PREFIX,
                            drrtl.Orchestrator.RUN_PREFIX)


class TestPortfolioAgents(unittest.TestCase):
    def test_specialists_are_told_to_stay_in_scope(self):
        sysmsg = portfolio.SPECIALIST_SYSTEM.lower()
        self.assertIn("scope", sysmsg)
        self.assertIn("byte-identical", sysmsg)

    def test_the_merge_agent_may_decline_to_include_everything(self):
        self.assertIn("not being asked to accept all of them",
                      portfolio.MERGE_SYSTEM)

    def test_agents_reuse_the_base_reply_parsers(self):
        for cls in (portfolio.RtlCleanupAgent, portfolio.PathSpecialistAgent):
            self.assertTrue(issubclass(cls, drrtl.RtlOptimizationAgent))

    def test_the_skill_document_reaches_every_agent(self):
        doc = "UNIQUE-SKILL-MARKER"
        for agent in (portfolio.RtlCleanupAgent("m", 1, doc),
                      portfolio.PathSpecialistAgent("m", 1, doc)):
            self.assertIn(doc, agent.system)
        self.assertIn(doc, portfolio.MergeAgent("m", 1, doc).system)

    def test_base_loop_prompts_are_untouched(self):
        """The addon must not change how `make opt` behaves."""
        a = drrtl.RtlOptimizationAgent("m", 1)
        self.assertEqual(a.system, drrtl.OPT_SYSTEM)
        self.assertEqual(a.directives, drrtl._DIRECTIVES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
