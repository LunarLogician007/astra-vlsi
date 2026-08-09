# ---------------------------------------------------------------------------
# PDK: Nangate 45nm Open Cell Library (FreePDK45)
#
# Default platform. Small, fast to synthesize/route, and the same library the
# Dr. RTL paper reports its Design Compiler numbers on, so timing numbers are
# comparable in spirit. Shipped inside the ORFS image.
# ---------------------------------------------------------------------------
set PDK_NAME  "nangate45"
set PDK_DIR   "$PDK_ROOT/nangate45"

# --- liberty ---------------------------------------------------------------
set LIB_FILE  [astra_pick "nangate45 typical liberty" \
                  "$PDK_DIR/lib/NangateOpenCellLibrary_typical.lib" \
                  "$PDK_DIR/lib/*typical*.lib" \
                  "$PDK_DIR/lib/*.lib"]
set LIB_FILES [list $LIB_FILE]

# --- physical --------------------------------------------------------------
set TECH_LEF  [astra_pick "nangate45 tech lef" \
                  "$PDK_DIR/lef/NangateOpenCellLibrary.tech.lef" \
                  "$PDK_DIR/lef/*tech*.lef"]
set SC_LEF    [astra_pick "nangate45 std-cell lef" \
                  "$PDK_DIR/lef/NangateOpenCellLibrary.macro.mod.lef" \
                  "$PDK_DIR/lef/*macro*.lef"]
set EXTRA_LEFS {}

# --- floorplan / place / route knobs --------------------------------------
set PLACE_SITE      "FreePDK45_38x28_10R_NP_162NW_34O"
set CORE_UTIL       40
set PLACE_DENSITY   0.60
set IO_LAYER_HOR    "metal5"
set IO_LAYER_VER    "metal6"
set MIN_ROUTE_LAYER "metal2"
set MAX_ROUTE_LAYER "metal10"

set CTS_BUF_CELL    "BUF_X4"
set FILL_CELLS      {FILLCELL_X1 FILLCELL_X2 FILLCELL_X4 FILLCELL_X8 FILLCELL_X16 FILLCELL_X32}
set TAP_CELL        "TAPCELL_X1"
set TIEHI_CELL      "LOGIC1_X1"
set TIELO_CELL      "LOGIC0_X1"
# `insert_tiecells` wants <cell>/<output port>.
set TIEHI_PORT      "LOGIC1_X1/Z"
set TIELO_PORT      "LOGIC0_X1/Z"

# Yosys technology mapping: DFF cell used by `dfflibmap`/`abc` comes from the
# liberty file itself, so nothing extra is needed here.
set YOSYS_ABC_DRIVER ""
