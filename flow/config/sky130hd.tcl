# ---------------------------------------------------------------------------
# PDK: SkyWater SKY130 high-density standard cells (sky130_fd_sc_hd)
#
# Use when you want a real, fabricable open PDK. Slower to route than
# nangate45 and the cells are considerably weaker, so expect worse WNS at the
# same clock period — do not compare slack across PDKs.
# ---------------------------------------------------------------------------
set PDK_NAME  "sky130hd"
set PDK_DIR   "$PDK_ROOT/sky130hd"

set LIB_FILE  [astra_pick "sky130hd typical liberty" \
                  "$PDK_DIR/lib/sky130_fd_sc_hd__tt_025C_1v80.lib" \
                  "$PDK_DIR/lib/*tt_025C*.lib" \
                  "$PDK_DIR/lib/*.lib"]
set LIB_FILES [list $LIB_FILE]

set TECH_LEF  [astra_pick "sky130hd tech lef" \
                  "$PDK_DIR/lef/sky130_fd_sc_hd.tlef" \
                  "$PDK_DIR/lef/*.tlef"]
set SC_LEF    [astra_pick "sky130hd std-cell lef" \
                  "$PDK_DIR/lef/sky130_fd_sc_hd_merged.lef" \
                  "$PDK_DIR/lef/*merged*.lef" \
                  "$PDK_DIR/lef/sky130_fd_sc_hd.lef"]
set EXTRA_LEFS {}

set PLACE_SITE      "unithd"
set CORE_UTIL       40
set PLACE_DENSITY   0.55
set IO_LAYER_HOR    "met3"
set IO_LAYER_VER    "met2"
set MIN_ROUTE_LAYER "met1"
set MAX_ROUTE_LAYER "met5"

set CTS_BUF_CELL    "sky130_fd_sc_hd__clkbuf_4"
set FILL_CELLS      {sky130_fd_sc_hd__fill_1 sky130_fd_sc_hd__fill_2
                     sky130_fd_sc_hd__fill_4 sky130_fd_sc_hd__fill_8}
set TAP_CELL        "sky130_fd_sc_hd__tapvpwrvgnd_1"
set TIEHI_CELL      "sky130_fd_sc_hd__conb_1"
set TIELO_CELL      "sky130_fd_sc_hd__conb_1"
set TIEHI_PORT      "sky130_fd_sc_hd__conb_1/HI"
set TIELO_PORT      "sky130_fd_sc_hd__conb_1/LO"

set YOSYS_ABC_DRIVER ""
