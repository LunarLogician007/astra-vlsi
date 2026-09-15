# ===========================================================================
# ASTRA — host-side helpers. Everything real happens inside the container.
#
#   make build                 build the image (native arch, no emulation)
#   make shell                 shell with this repo mounted at /work
#   make doctor                check tools + PDK
#   make run DESIGN=mac_chain  synthesis + timing report
#   make advise DESIGN=mac_chain  ask Claude how to close timing (runs on host)
#   make save                  export the image as a tarball for a teammate
#
# Dr. RTL optimisation loop:
#   make selftest              check the scoring maths (no container needed)
#   make localise DESIGN=x     map the critical path back onto RTL lines
#   make skills                show the learned skill library
#   make score DESIGN=x BASELINE=<run>   Eq. 3 score of a run
#   make opt DESIGN=x          the closed loop: analyse -> rewrite -> evaluate
#
# Path-portfolio addon (see README "Path-portfolio mode"):
#   make scan DESIGN=x         structural smells in the RTL, before any tool
#   make paths DESIGN=x        the distinct critical-path targets worth an agent
#   make skilldoc              build the RTL timing-optimisation skill document
#   make portfolio DESIGN=x    k scoped specialists, then a merge
#
# Variants:
#   make build PDKS="nangate45 sky130hd"        add another PDK
#   make build PLATFORM=linux/amd64             image for x86 teammates
#   make build DOCKERFILE=docker/Dockerfile.orfs  full image, adds OpenROAD PnR
#   make build EQY=0                            skip the eqy/sby build
# ===========================================================================

IMAGE      ?= astra:latest
DOCKERFILE ?= docker/Dockerfile
PDKS       ?= nangate45
DESIGN     ?= mac_chain
DOCKER     ?= docker
EQY        ?= 1

# Native by default. Set PLATFORM to cross-build (e.g. for an x86 teammate).
ifdef PLATFORM
PLATFORM_FLAG = --platform $(PLATFORM)
endif

RUN_FLAGS = --rm -it $(PLATFORM_FLAG) -v "$(CURDIR)":/work -w /work

# Same mount without a TTY: what the host-side orchestrator prepends to every
# EDA invocation. See tools/toolenv.py.
#
# DOCKER_MEM caps each tool container, so a runaway SAT run is killed inside
# its own container instead of taking the Colima VM (or WSL) down with every
# other job. Keep it below the VM's memory: DOCKER_MEM=6g on an 8 GB host.
DOCKER_MEM ?= 12g
TOOL_PREFIX = $(DOCKER) run --rm --memory=$(DOCKER_MEM) $(PLATFORM_FLAG) -v "$(CURDIR)":/work -w /work $(IMAGE)

.PHONY: build shell doctor run syn pnr list advise save clean-runs help \
        selftest localise skills score sec opt clean clean-skills \
        scan paths skilldoc portfolio

build:
	$(DOCKER) buildx build $(PLATFORM_FLAG) --build-arg PDKS="$(PDKS)" \
		--build-arg WITH_EQY="$(EQY)" \
		-t $(IMAGE) -f $(DOCKERFILE) . --load

shell:
	$(DOCKER) run $(RUN_FLAGS) $(IMAGE) /bin/bash

doctor:
	$(DOCKER) run $(RUN_FLAGS) $(IMAGE) astra doctor

run:
	$(DOCKER) run $(RUN_FLAGS) $(IMAGE) astra run $(DESIGN)

syn:
	$(DOCKER) run $(RUN_FLAGS) $(IMAGE) astra syn $(DESIGN)

pnr:
	$(DOCKER) run $(RUN_FLAGS) $(IMAGE) astra run $(DESIGN) --pnr

list:
	$(DOCKER) run $(RUN_FLAGS) $(IMAGE) astra list

# --- advisor ---------------------------------------------------------------
# Host-side, not containerised: the image has no `claude` binary and no
# credentials. Reads runs/<design>/latest/ (bind-mounted, so the host sees the
# same files) and writes advice.md next to the metrics.
#
#   make advise DESIGN=mac_chain
#   make advise DESIGN=mac_chain ADVISE_ARGS=--dry-run   prompt only, no call
#   make advise DESIGN=mac_chain ADVISE_ARGS=--force     re-spend a call
#
# One call per finished run. On a Claude subscription this draws from your
# plan limits rather than billing per token, so it is deliberately not wired
# into `make run`.
ADVISE_ARGS ?=

advise:
	python3 tools/astra_advise.py $(DESIGN) $(ADVISE_ARGS)

# --- Dr. RTL optimisation loop ---------------------------------------------
# `selftest` covers the paper's equations, the path-to-RTL mapper and the
# skill library. Pure Python, so it runs on the host with nothing installed.
selftest:
	python3 tools/selftest.py

localise:
	$(DOCKER) run $(RUN_FLAGS) $(IMAGE) astra localise $(DESIGN)

skills:
	python3 tools/skills.py list

# Eq. 3 needs something to be relative to:
#   make score DESIGN=mac_chain BASELINE=20260830-101500
BASELINE ?=
RUN      ?= latest
score:
	@test -n "$(BASELINE)" || { echo "set BASELINE=<run id>; see: make list"; exit 1; }
	$(DOCKER) run $(RUN_FLAGS) $(IMAGE) astra score $(DESIGN) \
		--run $(RUN) --baseline $(BASELINE)

SEC_ARGS ?=
sec:
	$(DOCKER) run $(RUN_FLAGS) $(IMAGE) astra sec $(SEC_ARGS)

# The loop is the one target that needs both halves at once: `claude` and its
# credentials on the host, Yosys/OpenSTA/eqy in the container. So it runs
# host-side and dispatches each tool call into the image via
# ASTRA_TOOL_PREFIX. Nothing is copied -- the repo is bind-mounted, so both
# sides read and write the same runs/ directory.
#
#   make opt DESIGN=mac_chain
#   make opt DESIGN=mac_chain OPT_ARGS="-n 6 --iters 4"
#   make opt DESIGN=mac_chain OPT_ARGS=--dry-run   prompts only, no model call
OPT_ARGS ?=
opt:
	ASTRA_TOOL_PREFIX='$(TOOL_PREFIX)' python3 tools/drrtl.py $(DESIGN) $(OPT_ARGS)

# --- path-portfolio addon --------------------------------------------------
# `scan`, `paths` and `skilldoc` are pure Python over files the flow already
# wrote, so they run on the host like `make skills` does -- no image needed.
#
#   make scan  DESIGN=mac_chain
#   make paths DESIGN=mac_chain RUN=<run id>
#   make skilldoc                       rebuild SKILL.md from the library
#   make skilldoc SKILLDOC_ARGS=--llm   one research call, then rebuild
PATHS_ARGS    ?=
SKILLDOC_ARGS ?=

scan:
	python3 tools/astra.py scan $(DESIGN)

paths:
	python3 tools/astra.py paths $(DESIGN) --run $(RUN) $(PATHS_ARGS)

skilldoc:
	python3 tools/skillgen.py build $(SKILLDOC_ARGS)

# Same split as `opt`: claude and its credentials on the host, Yosys/OpenSTA
# in the image. Note --dry-run still evaluates the baseline, so it still needs
# the container -- it skips the model calls, not the tools.
#
#   make portfolio DESIGN=mac_chain
#   make portfolio DESIGN=mac_chain PF_ARGS="--model haiku"
#   make portfolio DESIGN=mac_chain PF_ARGS="-k 2 --iters 1 --clean 0"
#   make portfolio DESIGN=mac_chain PF_ARGS=--dry-run    prompts only
PF_ARGS ?=

portfolio:
	ASTRA_TOOL_PREFIX='$(TOOL_PREFIX)' python3 tools/portfolio.py $(DESIGN) $(PF_ARGS)

save:
	$(DOCKER) save $(IMAGE) | gzip > astra-image.tar.gz
	@echo "wrote astra-image.tar.gz — load with: gunzip -c astra-image.tar.gz | docker load"

# --- cleanup ---------------------------------------------------------------
# clean       flow outputs only (safe, regenerate with `make run`)
# clean-cache Docker build cache + dangling images
# clean-all   the above plus the astra image itself (full rebuild after)
# reset-vm    macOS/Colima only: delete and recreate the VM. Freeing space
#             inside the VM does not shrink its disk file on the host, so this
#             is the only reliable way to get that space back.

# The skill library is deliberately NOT removed here: it is the accumulated
# result of every past run, and it is the one output that is supposed to
# outlive them. `make clean-skills` resets it on purpose.
clean:
	rm -rf runs/*
	find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete 2>/dev/null || true
	@echo "removed run outputs and python caches (skill library kept)"

clean-skills:
	git checkout -- skills/library.json 2>/dev/null \
		|| rm -f skills/library.json
	@echo "skill library reset to the shipped seed set"

clean-cache:
	-$(DOCKER) buildx prune -af
	-$(DOCKER) image prune -f
	@echo "removed build cache and dangling images"

clean-all: clean clean-cache
	-$(DOCKER) rmi -f $(IMAGE)
	@echo "removed $(IMAGE) — run 'make build' before using the flow again"

VM_CPU  ?= 6
VM_MEM  ?= 8
VM_DISK ?= 20

# `colima delete` leaves the persistent Docker data disk behind in
# ~/.colima/_lima/_disks/, which is where the space actually is — remove it
# explicitly. vz+rosetta keeps `--platform linux/amd64` builds working, so an
# x86 teammate's build failure can be reproduced on an Apple Silicon Mac.
reset-vm:
	-colima delete -f
	rm -rf ~/.colima/_lima/_disks/colima
	colima start --vm-type vz --vz-rosetta \
		--cpu $(VM_CPU) --memory $(VM_MEM) --disk $(VM_DISK)
	@echo "fresh VM: $(VM_CPU) cpu / $(VM_MEM) GB ram / $(VM_DISK) GB disk"

help:
	@grep -E '^#   ' $(MAKEFILE_LIST) | sed 's/^#   //'
