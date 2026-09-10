# ---------------------------------------------------------------------------
# Sequential equivalence check  (run with: yosys -c flow/scripts/sec.tcl)
#
# Proves that a rewritten design is functionally identical to the original --
# the SEC_i in {0,1} constraint of Eq. 4. A rewrite that fails here is
# discarded no matter how much slack it recovered.
#
# Inputs (environment, set by tools/sec.py):
#   ASTRA_FLOW        flow/ directory
#   ASTRA_TOP         top module name (must be the same on both sides)
#   ASTRA_GOLD_FILES  space-separated sources for the reference design D_0
#   ASTRA_GATE_FILES  space-separated sources for the candidate design D_i
#   ASTRA_SEC_DEPTH   bounded-check depth in cycles when induction fails
#   ASTRA_SEC_LIBERTY optional .lib -- set when the gate side is a netlist
#   ASTRA_SEC_WORKDIR where the proof transcripts are written
#
# Method: build an equivalence miter over the two designs and discharge it
# with Yosys's own SAT engine, hardest proof first.
#
#   1. temporal induction  -- holds for all reachable states, so a pass here
#      is a real proof rather than a bounded one. `-maxsteps` caps how long
#      the induction is allowed to search, not what it proves.
#   2. bounded check       -- N cycles from the reset state. Weaker: it can
#      only refute, or say "no counterexample within N". Reported honestly as
#      `bounded`, and the orchestrator treats bounded-only passes as such.
#
# Both sides are given the same zero initial state so the induction base case
# is well defined; the designs' own resets then take over from cycle 0.
# ---------------------------------------------------------------------------
source [file join $::env(ASTRA_FLOW) config common.tcl]

proc sec_kv {key value} { puts "ASTRA_KV sec.$key $value" }
proc sec_step {name}    { puts "###ASTRA_STEP $name" }
proc sec_ok {name}      { puts "###ASTRA_STEP_OK $name" }
proc sec_fail {name m}  { puts "###ASTRA_STEP_FAIL $name : $m" }

set top   $::env(ASTRA_TOP)
set gold  $::env(ASTRA_GOLD_FILES)
set gate  $::env(ASTRA_GATE_FILES)
set depth [astra_env ASTRA_SEC_DEPTH 20]
set lib   [astra_env ASTRA_SEC_LIBERTY ""]
set work  [astra_env ASTRA_SEC_WORKDIR "."]
# Multi-clock designs need every flop's clock turned into explicit edge-detect
# logic, so the solver is free to choose any clock waveform -- see below.
set multiclock [astra_env ASTRA_SEC_MULTICLOCK 0]
file mkdir $work

puts "###ASTRA_BEGIN sec_config"
puts "  top   : $top"
puts "  gold  : $gold"
puts "  gate  : $gate"
puts "  depth : $depth"
puts "###ASTRA_END sec_config"

# --- read both sides -------------------------------------------------------
# `prep -flatten` is what makes the two sides structurally comparable: it
# resolves processes and memories and collapses hierarchy, so the miter sees
# two flat state machines over the same port list.

sec_step read_gold
if {[catch {
    yosys design -reset
    foreach f $gold { yosys read_verilog -sv $f }
    yosys prep -flatten -top $top
    if {$multiclock} { yosys clk2fflogic }
    yosys design -stash gold_design
} msg]} {
    sec_fail read_gold $msg
    sec_kv equivalent 0
    sec_kv method error
    sec_kv reason "could not elaborate the reference design: $msg"
    return
}
sec_ok read_gold

sec_step read_gate
if {[catch {
    yosys design -reset
    if {$lib ne ""} { yosys read_liberty -lib $lib }
    foreach f $gate { yosys read_verilog -sv $f }
    yosys prep -flatten -top $top
    if {$multiclock} { yosys clk2fflogic }
    yosys design -stash gate_design
} msg]} {
    sec_fail read_gate $msg
    sec_kv equivalent 0
    sec_kv method error
    # A candidate that will not elaborate is a failed rewrite, not a tool
    # problem -- report it as a SEC failure so the loop discards it and the
    # skill learner records the strategy as invalid.
    sec_kv reason "candidate does not elaborate: $msg"
    return
}
sec_ok read_gate

# --- how a multi-clock design is handled ------------------------------------
# `sat` turns each $dff into "Q at step t+1 equals D at step t" and ignores the
# clock, which silently assumes every flop ticks together -- true of one clock,
# false of five. A verdict under that assumption is not a statement about an
# asynchronous design.
#
# `clk2fflogic` removes the assumption instead of working around it: every
# clocked flop becomes explicit edge-detection logic over an ordinary input, so
# the design has no clocks left and the solver chooses each clock's waveform
# freely. A pass then holds for EVERY interleaving of the five domains, which
# is what asynchrony means, rather than for the one interleaving the miter
# happened to assume.
#
# The cost is real and is reported rather than hidden. Free clocks mean
# temporal induction does not converge (measured: it returns neither way), so a
# multi-clock verdict is always `bounded`. And an edge now takes two steps
# instead of one, so N steps buy roughly N/2 clock cycles -- the depth is
# doubled below to keep the cycle count comparable.

# --- miter -----------------------------------------------------------------
sec_step miter
if {[catch {
    yosys design -reset
    yosys design -copy-from gold_design -as gold $top
    yosys design -copy-from gate_design -as gate $top
    yosys miter -equiv -flatten -make_assert -make_outputs gold gate sec_miter
    yosys hierarchy -top sec_miter
    yosys opt -full
} msg]} {
    sec_fail miter $msg
    sec_kv equivalent 0
    sec_kv method error
    # Mismatched port lists land here. That is a real rejection: the paper's
    # setting preserves the module interface and the pipeline latency.
    sec_kv reason "cannot build an equivalence miter (port list or interface changed?): $msg"
    return
}
sec_ok miter

# --- proofs ----------------------------------------------------------------
# `sat -verify` is deliberately NOT used. It turns a failed proof into a Yosys
# *log_error*, which tears the process down instead of raising a catchable Tcl
# error -- so the script would die before it could report a verdict or try the
# bounded fallback, and a genuine inequivalence would be indistinguishable
# from a crashed tool. Without it, `sat` returns normally and states the
# outcome in its own output, which is what gets read back here via `tee`.

proc sec_prove {label file args} {
    # Returns: pass | fail | error
    if {[catch { yosys tee -o $file sat {*}$args } msg]} {
        puts "  sat ($label) raised: $msg"
        if {![file exists $file]} { return error }
    }
    if {[catch { set fh [open $file r] }]} { return error }
    set txt [read $fh]
    close $fh
    # Yosys prints "... : SUCCESS!" or "... : FAIL!" as its final verdict.
    if {[string match "*SUCCESS!*" $txt]} { return pass }
    if {[string match "*FAIL!*" $txt]}    { return fail }
    return error
}

# --- proof attempt 1: temporal induction (unbounded) -----------------------
# Skipped under multiclock: with free clocks induction returns neither pass nor
# fail, so running it only spends the time budget before the bounded check.
if {$multiclock} {
    set steps [expr {$depth * 2}]
    sec_step bounded
    puts "###ASTRA_BEGIN sec_bounded"
    set r [sec_prove bounded [file join $work bounded.txt] \
               -prove-asserts -set-init-zero -seq $steps]
    puts "###ASTRA_END sec_bounded"
    sec_kv depth $depth
    sec_kv steps $steps
    sec_kv multiclock 1
    if {$r eq "pass"} {
        sec_ok bounded
        sec_kv equivalent 1
        sec_kv method bounded
        sec_kv reason "no counterexample in $steps steps (about $depth cycles) with every clock free; bounded, not a proof"
        return
    }
    sec_fail bounded "bounded multiclock check returned $r"
    sec_kv equivalent 0
    sec_kv method bounded
    if {$r eq "fail"} {
        sec_kv reason "counterexample found within $steps steps: not equivalent under some clock interleaving"
    } else {
        sec_kv reason "a $steps-step bounded check with free clocks could not decide"
    }
    return
}

sec_step induction
puts "###ASTRA_BEGIN sec_induction"
set r [sec_prove induction [file join $work induction.txt] \
           -prove-asserts -tempinduct -set-init-zero -maxsteps $depth]
puts "###ASTRA_END sec_induction"

if {$r eq "pass"} {
    sec_ok induction
    sec_kv equivalent 1
    sec_kv method induction
    sec_kv depth $depth
    sec_kv reason "temporal induction proved the miter over all reachable states"
    return
}
sec_fail induction "temporal induction returned $r"

if {$r eq "fail"} {
    # A counterexample is definitive: the designs differ. There is nothing a
    # weaker check could add, so do not run one.
    sec_kv equivalent 0
    sec_kv method induction
    sec_kv reason "counterexample found: the candidate is not equivalent"
    return
}

# --- proof attempt 2: bounded check from reset -----------------------------
# Reached only when induction could not close either way -- which is not
# evidence of inequivalence, so it is worth asking the weaker question.
sec_step bounded
puts "###ASTRA_BEGIN sec_bounded"
set r [sec_prove bounded [file join $work bounded.txt] \
           -prove-asserts -set-init-zero -seq $depth]
puts "###ASTRA_END sec_bounded"

if {$r eq "pass"} {
    sec_ok bounded
    sec_kv equivalent 1
    sec_kv method bounded
    sec_kv depth $depth
    sec_kv reason "no counterexample within $depth cycles from reset (bounded, not a proof)"
    return
}
sec_fail bounded "bounded check returned $r"
sec_kv equivalent 0
sec_kv method bounded
sec_kv depth $depth
if {$r eq "fail"} {
    sec_kv reason "counterexample found within $depth cycles: not equivalent"
} else {
    sec_kv reason "neither induction nor a $depth-cycle bounded check could decide"
}
