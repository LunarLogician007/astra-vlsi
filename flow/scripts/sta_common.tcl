# ---------------------------------------------------------------------------
# Shared OpenSTA reporting procs.
#
# Everything a downstream parser needs is emitted on stdout in two forms:
#   ASTRA_KV <key> <value>        single machine-readable scalars
#   ###ASTRA_BEGIN/END <section>  delimited blocks of human-readable report
# tools/parse_sta.py turns both into JSON. Nothing here relies on OpenSTA's
# file-redirection syntax, so the same script runs under `sta` and under
# `openroad -no_init`.
# ---------------------------------------------------------------------------

proc astra_kv {key value} {
    puts "ASTRA_KV $key $value"
}

# OpenSTA's Tcl API returns time in seconds; reports print in library units.
# Convert once, here, so every consumer sees nanoseconds.
proc astra_to_ns {seconds} {
    if {$seconds eq "" || $seconds eq "INF" || $seconds eq "-INF"} { return "" }
    return [expr {$seconds * 1e9}]
}

# Worst negative slack / total negative slack for one path delay type.
proc astra_slack_summary {prefix path_delay} {
    set ws ""
    set tns ""
    if {[catch {sta::worst_slack -$path_delay} ws]} {
        # Older/newer builds: fall back to scanning the report text.
        set ws ""
    }
    if {[catch {sta::total_negative_slack -$path_delay} tns]} {
        set tns ""
    }
    if {$ws ne ""} { astra_kv "$prefix.wns_ns" [astra_to_ns $ws] }
    if {$tns ne ""} { astra_kv "$prefix.tns_ns" [astra_to_ns $tns] }

    # Count of failing endpoints.
    set nviol 0
    set ok 0
    if {![catch {
        set viol [find_timing_paths -path_delay $path_delay -slack_max 0 \
                                    -group_path_count 100000 -endpoint_path_count 1]
        set nviol [llength $viol]
        set ok 1
    }]} {
        # ok
    } elseif {![catch {
        # OpenSTA < 2.6 spelling.
        set viol [find_timing_paths -path_delay $path_delay -slack_max 0 \
                                    -group_count 100000 -endpoint_count 1]
        set nviol [llength $viol]
        set ok 1
    }]} {
        # ok
    }
    if {$ok} { astra_kv "$prefix.violating_endpoints" $nviol }
}

# Per-clock-group worst/total negative slack.
#
# A single design-wide WNS says nothing about which domain owns it. Once a
# design has more than one clock, every consumer that divides a slack by "the
# period" divides four fifths of the paths by the wrong number, and the
# mis-ranking is silent -- see HANDOFF.md section 6.1. Paths are bucketed by
# their capture clock, which is what OpenSTA names a path group after, so the
# name emitted here joins directly onto the `Path Group:` field the report
# already prints per path.
#
# Everything is guarded: on a build where path objects do not expose `slack`
# via get_property, no group KVs are emitted and the Python side falls back to
# bucketing the reported paths itself.
proc astra_group_summary {prefix path_delay} {
    if {[catch {all_clocks} clks]} { return }
    foreach clk $clks {
        set name ""
        if {[catch {get_name $clk} name]} {
            if {[catch {get_property $clk name} name]} { continue }
        }
        if {$name eq ""} { continue }

        set paths {}
        if {[catch {
            set paths [find_timing_paths -path_delay $path_delay \
                           -to [get_clocks $name] \
                           -group_path_count 100000 -endpoint_path_count 1]
        }]} {
            # OpenSTA < 2.6 spelling.
            if {[catch {
                set paths [find_timing_paths -path_delay $path_delay \
                               -to [get_clocks $name] \
                               -group_count 100000 -endpoint_count 1]
            }]} { continue }
        }

        set wns ""
        set tns 0.0
        set nviol 0
        set nread 0
        foreach p $paths {
            set s ""
            if {[catch {get_property $p slack} s]} { continue }
            if {$s eq "" || $s eq "INF" || $s eq "-INF"} { continue }
            incr nread
            if {$wns eq "" || $s < $wns} { set wns $s }
            if {$s < 0} {
                set tns [expr {$tns + $s}]
                incr nviol
            }
        }
        astra_kv "$prefix.group.$name.endpoints" [llength $paths]
        if {$nread > 0} {
            # NOT astra_to_ns. `sta::worst_slack` hands back seconds, but
            # `get_property <path> slack` is already in library time units --
            # the same units report_checks prints, which is what parse_paths
            # reads per path. Converting here produced 1e9-times-too-large
            # numbers that the parser's magnitude guard happened to rescue;
            # that guard only fires above 1e6, so a slack under 1 ps would
            # have sailed through it and been recorded as tens of thousands
            # of nanoseconds.
            astra_kv "$prefix.group.$name.wns_ns" $wns
            astra_kv "$prefix.group.$name.tns_ns" $tns
            astra_kv "$prefix.group.$name.violating_endpoints" $nviol
        }
    }
}

# One report_checks call, tolerating the option rename that happened in
# OpenSTA (-group_count -> -group_path_count).
proc astra_checks {delay n} {
    set common "-path_delay $delay -sort_by_slack -format full_clock_expanded \
                -fields {slew cap input_pins fanout} -digits 4"
    if {[catch {eval report_checks -group_path_count $n -endpoint_path_count 1 $common} msg]} {
        if {[catch {eval report_checks -group_count $n -endpoint_count 1 $common} msg2]} {
            puts "report_checks failed: $msg2"
        }
    }
}

# `n` worst paths, expanded enough to trace the critical path back to RTL:
# pin names, cell types, fanout, transition, capacitance.
proc astra_report_paths {tag n} {
    puts "###ASTRA_BEGIN ${tag}_paths"
    astra_checks max $n
    puts "###ASTRA_END ${tag}_paths"

    puts "###ASTRA_BEGIN ${tag}_min_paths"
    astra_checks min 5
    puts "###ASTRA_END ${tag}_min_paths"
}

# Design-rule and clocking health, the usual companions to a slack number.
proc astra_report_checks_extra {tag} {
    puts "###ASTRA_BEGIN ${tag}_check_types"
    catch { report_check_types -max_slew -max_capacitance -max_fanout -violators }
    puts "###ASTRA_END ${tag}_check_types"

    puts "###ASTRA_BEGIN ${tag}_clock_skew"
    catch { report_clock_skew }
    puts "###ASTRA_END ${tag}_clock_skew"

    puts "###ASTRA_BEGIN ${tag}_tns_wns"
    catch { report_worst_slack -max }
    catch { report_tns }
    catch { report_wns }
    puts "###ASTRA_END ${tag}_tns_wns"
}

# One call that produces the whole timing artifact set for a stage.
proc astra_timing_report {tag {npaths 20}} {
    astra_slack_summary $tag max
    astra_slack_summary "${tag}.hold" min
    astra_group_summary $tag max
    astra_report_paths $tag $npaths
    astra_report_checks_extra $tag
}
