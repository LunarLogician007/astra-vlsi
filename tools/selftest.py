#!/usr/bin/env python3
"""Self-tests for the Dr. RTL layer: the paper's equations, the RTL mapper,
the skill library, and the agent-reply parsers.

None of this needs Yosys, OpenSTA or a model, which is the point -- the parts
that decide which rewrite wins should be checkable without a 1 GB container.

    python3 tools/selftest.py            # or: make selftest
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import drrtl        # noqa: E402
import rtl_map      # noqa: E402
import score        # noqa: E402
import skills       # noqa: E402


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
