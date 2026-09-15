# ---------------------------------------------------------------------------
# Register-correspondence SEC  (run with: yosys -c flow/scripts/sec_regcorr.tcl)
#
# The Yosys half of the `regcorr` stage. tools/regcorr.py is the other half,
# and docs/equivalence-contract.md says why the combination is a proof for
# every clock interleaving. tools/sec.py drives the two phases:
#
#   elaborate  read each side, `proc; flatten`, and write it out as JSON.
#              Deliberately no `opt`: register names are what the pairing
#              reads, and opt_merge could fold two registers into one on one
#              side only.
#
#   prove      read the combinational cones tools/regcorr.py cut out (modules
#              `gold` and `gate`), match every same-named signal with
#              `equiv_make`, and discharge each match with `equiv_simple`.
#              There are no flops left in these modules -- that is asserted --
#              so equiv_simple is doing plain combinational equivalence, with
#              matched internal names as cut points. If internal names block
#              the proof (a candidate reused a name for something else), it is
#              tried once more with only the ports matched.
#
# Inputs (environment, set by tools/sec.py):
#   ASTRA_FLOW              flow/ directory
#   ASTRA_TOP               top module name
#   ASTRA_GOLD_FILES        space-separated reference sources   (elaborate)
#   ASTRA_GATE_FILES        space-separated candidate sources   (elaborate)
#   ASTRA_SEC_WORKDIR       where JSON and transcripts go
#   ASTRA_REGCORR_PHASE     elaborate | prove
#
# A pass here requires every $equiv proven: `equiv_status` is read back and
# anything unproven is reported as such. Nothing here ever reports a
# refutation -- an unproven port may be a renamed register -- so a failure is
# undecided and tools/sec.py moves on to the bounded check.
# ---------------------------------------------------------------------------
source [file join $::env(ASTRA_FLOW) config common.tcl]

proc sec_kv {key value} { puts "ASTRA_KV sec.$key $value" }
proc sec_step {name}    { puts "###ASTRA_STEP $name" }
proc sec_ok {name}      { puts "###ASTRA_STEP_OK $name" }
proc sec_fail {name m}  { puts "###ASTRA_STEP_FAIL $name : $m" }

set top   $::env(ASTRA_TOP)
set work  [astra_env ASTRA_SEC_WORKDIR "."]
set phase [astra_env ASTRA_REGCORR_PHASE "elaborate"]
file mkdir $work

if {$phase eq "elaborate"} {
    foreach side {gold gate} var {ASTRA_GOLD_FILES ASTRA_GATE_FILES} {
        sec_step elaborate_$side
        if {[catch {
            yosys design -reset
            foreach f $::env($var) { yosys read_verilog -sv $f }
            yosys hierarchy -check -top $top
            yosys proc
            yosys flatten
            yosys opt_clean
            # Fold each tree of additions into one $macc. A serial accumulate
            # and a balanced adder tree over the same terms then differ only
            # in term order, which tools/regcorr.py hashes order-free -- so a
            # rewrite a solver cannot prove in 300 s bit by bit (260-bit,
            # 16-term, measured on netproc) matches with no solver at all.
            # It is a semantics-preserving rewrite applied alike to both sides.
            yosys alumacc
            yosys opt_clean
            yosys write_json [file join $work regcorr_$side.json]
        } msg]} {
            sec_fail elaborate_$side $msg
            sec_kv equivalent 0
            sec_kv method error
            if {$side eq "gold"} {
                sec_kv reason "could not elaborate the reference design: $msg"
            } else {
                sec_kv reason "candidate does not elaborate: $msg"
            }
            return
        }
        sec_ok elaborate_$side
    }
    sec_kv elaborated 1
    return
}

# --- prove ------------------------------------------------------------------

proc regcorr_status {file} {
    # Returns {total unproven}; {-1 -1} when the status could not be read.
    if {[catch { set fh [open $file r] }]} { return {-1 -1} }
    set txt [read $fh]
    close $fh
    if {[regexp {Of those cells ([0-9]+) are proven and ([0-9]+) are unproven} \
             $txt -> proven unproven]} {
        return [list [expr {$proven + $unproven}] $unproven]
    }
    if {[regexp {Found 0 \$equiv cells} $txt]} { return {0 0} }
    return {-1 -1}
}

proc regcorr_attempt {label purge work} {
    yosys design -load regcorr_cones
    if {$purge} {
        # Drop every internal name so only the ports are matched.
        yosys opt_clean -purge
    }
    yosys equiv_make gold gate equiv
    yosys hierarchy -top equiv
    yosys equiv_simple
    set file [file join $work regcorr_status_$label.txt]
    yosys tee -q -o $file equiv_status
    return [regcorr_status $file]
}

sec_step prove
if {[catch {
    yosys design -reset
    yosys read_json [file join $work gold_cone.json]
    yosys read_json [file join $work gate_cone.json]
    # regcorr.py cuts every flop open. If one survived, equiv_simple would
    # reason through it as if every clock ticked together -- exactly the
    # assumption this stage exists to avoid -- so refuse rather than risk it.
    yosys select -assert-none t:\$*ff* t:\$*dff* t:\$*latch* t:\$mem* t:\$sr \
        t:\$_SR_* t:\$*DFF* t:\$*LATCH*
    yosys design -save regcorr_cones
} msg]} {
    sec_fail prove $msg
    sec_kv equivalent 0
    sec_kv method error
    sec_kv reason "regcorr could not load the cones: $msg"
    return
}

set label named
if {[catch { set st [regcorr_attempt named 0 $work] } msg]} {
    puts "  regcorr (named) raised: $msg"
    set st {-1 -1}
}
if {[lindex $st 0] <= 0 || [lindex $st 1] != 0} {
    set label ports
    if {[catch { set st [regcorr_attempt ports 1 $work] } msg]} {
        puts "  regcorr (ports) raised: $msg"
        set st {-1 -1}
    }
}

lassign $st total unproven
sec_kv attempt $label
sec_kv equiv_cells $total
sec_kv unproven $unproven
if {$total > 0 && $unproven == 0} {
    sec_ok prove
    sec_kv equivalent 1
    sec_kv method regcorr
    sec_kv reason "register correspondence: all $total matched signals in the differing cones proven ($label); unbounded, holds for every clock interleaving"
} else {
    sec_fail prove "unproven $unproven of $total"
    sec_kv equivalent 0
    sec_kv method regcorr-unproven
    if {$total < 0} {
        sec_kv reason "regcorr could not read an equiv_status verdict"
    } else {
        sec_kv reason "register correspondence left $unproven of $total matched signals unproven; undecided, not a refutation"
    }
}
