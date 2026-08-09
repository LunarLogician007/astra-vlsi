# Resolve and validate the PDK paths for one platform.
#   sta -exit flow/scripts/doctor.tcl   (or: openroad -no_init -exit ...)
# Env: ASTRA_FLOW, ASTRA_PDK_CONFIG
source [file join $::env(ASTRA_FLOW) config common.tcl]

# A platform that simply was not installed is not a broken setup — say so
# before trying to resolve any file inside it.
set stem [file rootname [file tail $::env(ASTRA_PDK_CONFIG)]]
if {![file isdirectory [file join $PDK_ROOT $stem]]} {
    puts "  not installed (rebuild with --build-arg PDKS=\"$stem ...\")"
    puts "ASTRA_KV pdk.installed 0"
    puts "ASTRA_KV pdk.ok 1"
    exit 0
}
puts "ASTRA_KV pdk.installed 1"

if {[catch {source $::env(ASTRA_PDK_CONFIG)} err]} {
    puts "  ERROR   $err"
    puts "ASTRA_KV pdk.ok 0"
    exit 0
}

set ok 1
foreach {label path} [list liberty $LIB_FILE tech_lef $TECH_LEF sc_lef $SC_LEF] {
    if {[file exists $path]} {
        puts "  OK      $label : $path"
    } else {
        puts "  MISSING $label : $path"
        set ok 0
    }
}

# A liberty that parses is worth more than a liberty that merely exists.
if {[catch {read_liberty $LIB_FILE} err]} {
    puts "  ERROR   liberty failed to parse: $err"
    set ok 0
} else {
    puts "  OK      liberty parsed"
}

puts "ASTRA_KV pdk.name $PDK_NAME"
puts "ASTRA_KV pdk.ok $ok"
