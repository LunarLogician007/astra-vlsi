# Shared helpers for ASTRA PDK config files.
# Sourced by every Yosys / OpenSTA / OpenROAD script before the PDK file.

# Return the first path matching any of the given glob patterns, or error out
# with a message that names every pattern that was tried. PDK file names drift
# between ORFS releases, so all config paths go through this.
proc astra_pick {label args} {
    foreach pat $args {
        set hits [lsort [glob -nocomplain $pat]]
        if {[llength $hits] > 0} {
            return [lindex $hits 0]
        }
    }
    error "ASTRA: could not locate '$label'. Tried:\n  [join $args "\n  "]"
}

# Same, but returns every match (e.g. multi-corner liberty sets).
proc astra_pick_all {label args} {
    set out {}
    foreach pat $args {
        foreach h [lsort [glob -nocomplain $pat]] {
            if {[lsearch -exact $out $h] < 0} { lappend out $h }
        }
    }
    if {[llength $out] == 0} {
        error "ASTRA: could not locate any '$label'. Tried:\n  [join $args "\n  "]"
    }
    return $out
}

proc astra_env {name default} {
    if {[info exists ::env($name)] && $::env($name) ne ""} {
        return $::env($name)
    }
    return $default
}

set PDK_ROOT [astra_env ASTRA_PDK_ROOT /OpenROAD-flow-scripts/flow/platforms]
