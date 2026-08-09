# ============================================================================
# ASTRA — lean VLSI container: synthesis + timing.
#
#   Yosys      logic synthesis          (apt)
#   OpenSTA    static timing analysis   (source build, ~5 min)
#   PDK        nangate45 only, ~100 MB  (sparse-checkout from ORFS)
#
# Roughly 1-2 GB, and it builds natively on both arm64 and x86 — no emulation.
# The heavyweight `openroad/orfs` base (Yosys + OpenROAD + four PDKs, 20 GB+)
# is kept in docker/Dockerfile.orfs for when place & route is actually needed.
#
#   docker buildx build -t astra:latest -f docker/Dockerfile . --load
# ============================================================================
FROM ubuntu:24.04

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV DEBIAN_FRONTEND=noninteractive

# Build deps for OpenSTA, plus Yosys itself.
RUN apt-get update && apt-get install -y --no-install-recommends \
        yosys \
        git curl ca-certificates build-essential cmake \
        bison flex libfl-dev swig tcl-dev tk-dev libreadline-dev \
        zlib1g-dev libeigen3-dev \
        autoconf automake libtool \
        python3 jq less vim-tiny \
    && rm -rf /var/lib/apt/lists/*

# The flow drives Yosys through its Tcl interpreter (`yosys -c`). Prove that
# works now rather than discovering it on the first synthesis run.
RUN printf 'yosys log "astra: yosys tcl mode ok"\n' > /tmp/tcl_check.tcl \
    && yosys -c /tmp/tcl_check.tcl | grep -q "astra: yosys tcl mode ok" \
    && rm /tmp/tcl_check.tcl

# --- CUDD ------------------------------------------------------------------
# OpenSTA links against CUDD (BDD package) and Ubuntu does not package it.
RUN git clone --depth 1 https://github.com/ivmai/cudd.git /tmp/cudd \
    && cd /tmp/cudd \
    && autoreconf -i \
    && ./configure --prefix=/usr/local \
    && make -j"$(nproc)" \
    && make install \
    && cd / && rm -rf /tmp/cudd

# --- OpenSTA ---------------------------------------------------------------
# Pinned to a commit so every teammate builds the same STA (upstream publishes
# no release tags).
ARG OPENSTA_SHA=3f4b337e30afccf8075118860daf2e4fea8a5c18
RUN set -euo pipefail; \
    mkdir -p /tmp/OpenSTA && cd /tmp/OpenSTA; \
    git init -q; \
    git remote add origin https://github.com/parallaxsw/OpenSTA.git; \
    git fetch -q --depth 1 origin "${OPENSTA_SHA}"; \
    git checkout -q FETCH_HEAD; \
    # LTO makes gcc-13 hit an internal compiler error ("lto1: internal
    # compiler error: in return_token") building the `sta` binary on x86_64.
    # Upstream sets this as a normal variable, so -D on the command line
    # cannot override it — patch the line and fail loudly if it ever moves.
    sed -i 's/\(CMAKE_INTERPROCEDURAL_OPTIMIZATION_RELEASE\) TRUE/\1 FALSE/' CMakeLists.txt; \
    grep -q "CMAKE_INTERPROCEDURAL_OPTIMIZATION_RELEASE FALSE" CMakeLists.txt \
        || { echo "OpenSTA CMakeLists changed: re-check the LTO workaround" >&2; exit 1; }; \
    cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DCUDD_DIR=/usr/local; \
    cmake --build build -j"$(nproc)"; \
    cmake --install build; \
    ldconfig; \
    cd / && rm -rf /tmp/OpenSTA; \
    sta -version; \
    echo "opensta=${OPENSTA_SHA} (LTO disabled)" >> /etc/astra-tools.txt

# --- PDK -------------------------------------------------------------------
# Only the platform directories named in PDKS are fetched. Add sky130hd here
# if you want it: --build-arg PDKS="nangate45 sky130hd".
ARG PDKS="nangate45"
RUN set -euo pipefail; \
    # Big clones over HTTP/2 intermittently abort with "RPC failed; curl 92
    # ... CANCEL". HTTP/1.1 plus a retry loop makes this survive a flaky
    # connection instead of failing the whole build.
    git config --global http.version HTTP/1.1; \
    git config --global http.postBuffer 524288000; \
    paths=""; for p in ${PDKS}; do paths="$paths flow/platforms/$p"; done; \
    fetch_pdks() { \
        rm -rf /tmp/orfs; \
        git clone --filter=blob:none --no-checkout --depth 1 \
            https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts.git /tmp/orfs || return 1; \
        cd /tmp/orfs || return 1; \
        git sparse-checkout init --cone || return 1; \
        git sparse-checkout set $paths || return 1; \
        git checkout || return 1; \
    }; \
    ok=0; \
    for attempt in 1 2 3; do \
        if fetch_pdks; then ok=1; break; fi; \
        echo "PDK fetch attempt $attempt failed; retrying in 5s" >&2; sleep 5; \
    done; \
    [ "$ok" = 1 ] || { echo "could not fetch PDK files after 3 attempts" >&2; exit 1; }; \
    cd /tmp/orfs; \
    mkdir -p /pdks; \
    for p in ${PDKS}; do \
        [ -d "flow/platforms/$p" ] || { echo "no such platform: $p" >&2; exit 1; }; \
        cp -r "flow/platforms/$p" /pdks/; \
    done; \
    cd /; rm -rf /tmp/orfs; \
    echo "pdks=${PDKS}" >> /etc/astra-tools.txt; \
    du -sh /pdks/*

ENV ASTRA_ROOT=/work \
    ASTRA_PDK_ROOT=/pdks \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /work

# Baked in so the image works standalone; bind-mounting the repo over /work
# shadows this copy and picks up host edits without a rebuild.
COPY flow    /work/flow
COPY tools   /work/tools
COPY designs /work/designs

# A wrapper rather than a symlink: when /work is a bind mount from a Windows
# drive, the exec bit on the mounted file is gone, so invoking python3
# explicitly is the only thing that reliably works.
RUN chmod +x /work/tools/*.py \
    && printf '#!/bin/bash\nexec python3 /work/tools/astra.py "$@"\n' > /usr/local/bin/astra \
    && chmod +x /usr/local/bin/astra

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["/bin/bash"]
