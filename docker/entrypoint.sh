#!/bin/bash
set -euo pipefail

if [ -z "${ASTRA_QUIET:-}" ]; then
    echo "=================================================================="
    echo " ASTRA VLSI container"
    echo "   yosys : $(yosys -V 2>/dev/null | head -1 || echo 'NOT FOUND')"
    echo "   sta   : $(sta -version 2>/dev/null | head -1 || echo 'NOT FOUND')"
    echo "   sec   : $(command -v eqy >/dev/null && echo 'eqy' || echo 'yosys miter (eqy not installed)')"
    echo "   pdks  : $(ls "${ASTRA_PDK_ROOT:-/pdks}" 2>/dev/null | tr '\n' ' ')"
    echo "   'astra doctor' to check the setup, 'astra run <design>' to go"
    echo "   'astra opt <design>' for the Dr. RTL optimisation loop"
    echo "=================================================================="
fi

exec "$@"
