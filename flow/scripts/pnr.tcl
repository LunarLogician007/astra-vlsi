# ---------------------------------------------------------------------------
# OpenROAD place & route  (openroad -no_init -exit flow/scripts/pnr.tcl)
#
# A deliberately "lite" RTL-to-routed flow: floorplan -> pin placement ->
# global place -> repair -> detailed place -> CTS -> repair -> fillers ->
# global route -> (optional) detailed route -> post-route STA.
#
# Inputs (environment): ASTRA_FLOW, ASTRA_PDK_CONFIG, ASTRA_TOP, ASTRA_NETLIST,
#   ASTRA_SDC, ASTRA_OUT_DIR, ASTRA_NPATHS,
#   ASTRA_DETAILED_ROUTE (0/1, default 0 — slow),
#   ASTRA_REPAIR_TIMING  (0/1, default 1)
#
# Each optional step is wrapped in a reported catch: a PDK that lacks a tapcell
# or a design with no macros should degrade to a warning, not kill the run.
# ---------------------------------------------------------------------------
source [file join $::env(ASTRA_FLOW) config common.tcl]
source $::env(ASTRA_PDK_CONFIG)
source [file join $::env(ASTRA_FLOW) scripts sta_common.tcl]

set top       $::env(ASTRA_TOP)
set netlist   $::env(ASTRA_NETLIST)
set sdc       $::env(ASTRA_SDC)
set out_dir   $::env(ASTRA_OUT_DIR)
set npaths    [astra_env ASTRA_NPATHS 20]
set do_droute [astra_env ASTRA_DETAILED_ROUTE 0]
set do_repair [astra_env ASTRA_REPAIR_TIMING 1]

file mkdir $out_dir

proc astra_step {name body} {
    puts "###ASTRA_STEP $name"
    if {[catch {uplevel 1 $body} err]} {
        puts "###ASTRA_STEP_FAIL $name : $err"
        return 0
    }
    puts "###ASTRA_STEP_OK $name"
    return 1
}

# --- read design -----------------------------------------------------------
read_lef $TECH_LEF
read_lef $SC_LEF
foreach l $EXTRA_LEFS { read_lef $l }
foreach lib $LIB_FILES { read_liberty $lib }
read_verilog $netlist
link_design $top
read_sdc $sdc

# --- floorplan -------------------------------------------------------------
astra_step floorplan {
    initialize_floorplan -utilization $::CORE_UTIL -aspect_ratio 1.0 \
                         -core_space 2.0 -site $::PLACE_SITE
}
astra_step tracks {
    set mt [glob -nocomplain [file join $::PDK_DIR make_tracks.tcl]]
    if {[llength $mt] > 0} { source [lindex $mt 0] } else { make_tracks }
}
astra_step io_pins {
    place_pins -hor_layers $::IO_LAYER_HOR -ver_layers $::IO_LAYER_VER
}
astra_step tapcells {
    tapcell -distance 20 -tapcell_master $::TAP_CELL
}
astra_step tie_cells {
    insert_tiecells "$::TIEHI_PORT"
    insert_tiecells "$::TIELO_PORT"
}

# --- placement -------------------------------------------------------------
astra_step global_place {
    global_placement -density $::PLACE_DENSITY -pad_left 2 -pad_right 2
}
astra_step estimate_parasitics_place { estimate_parasitics -placement }
astra_step repair_design            { repair_design }
astra_step detailed_place           { detailed_placement }

# --- clock tree ------------------------------------------------------------
astra_step cts {
    clock_tree_synthesis -buf_list $::CTS_BUF_CELL -root_buf $::CTS_BUF_CELL \
                         -sink_clustering_enable
}
astra_step propagate_clocks {
    set_propagated_clock [all_clocks]
}
astra_step post_cts_place { detailed_placement }
astra_step estimate_parasitics_cts { estimate_parasitics -placement }

if {$do_repair} {
    astra_step repair_timing_setup { repair_timing -setup }
    astra_step repair_timing_hold  { repair_timing -hold }
    astra_step post_repair_place   { detailed_placement }
}

astra_step fillers { filler_placement $::FILL_CELLS }

# --- routing ---------------------------------------------------------------
astra_step routing_layers {
    set_routing_layers -signal "$::MIN_ROUTE_LAYER-$::MAX_ROUTE_LAYER" \
                       -clock  "$::MIN_ROUTE_LAYER-$::MAX_ROUTE_LAYER"
}
astra_step global_route {
    global_route -guide_file [file join $out_dir route.guide] \
                 -congestion_iterations 30
}
astra_step estimate_parasitics_gr { estimate_parasitics -global_routing }

# Post-global-route timing: parasitics are real estimates now, so this is the
# number to compare against the post-synthesis WNS.
astra_timing_report post_gr $npaths

if {$do_droute} {
    astra_step detailed_route {
        detailed_route -output_drc [file join $out_dir route.drc] -verbose 0
    }
    astra_step estimate_parasitics_dr { estimate_parasitics -global_routing }
    astra_timing_report post_route $npaths
}

# --- outputs ---------------------------------------------------------------
astra_step write_outputs {
    write_def     [file join $out_dir final.def]
    write_verilog [file join $out_dir final_netlist.v]
    write_db      [file join $out_dir final.odb]
}
astra_step write_spef {
    write_spef [file join $out_dir final.spef]
}

puts "###ASTRA_BEGIN pnr_area"
catch { report_design_area }
puts "###ASTRA_END pnr_area"

puts "###ASTRA_BEGIN pnr_utilization"
catch { report_units }
catch { report_cell_usage }
puts "###ASTRA_END pnr_utilization"

astra_kv "pnr.def" [file join $out_dir final.def]
astra_kv "pnr.odb" [file join $out_dir final.odb]
astra_kv "pnr.pdk" $PDK_NAME
