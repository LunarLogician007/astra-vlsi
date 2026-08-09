# ===========================================================================
# ASTRA — host-side helpers. Everything real happens inside the container.
#
#   make build                 build the image (native arch, no emulation)
#   make shell                 shell with this repo mounted at /work
#   make doctor                check tools + PDK
#   make run DESIGN=mac_chain  synthesis + timing report
#   make save                  export the image as a tarball for a teammate
#
# Variants:
#   make build PDKS="nangate45 sky130hd"        add another PDK
#   make build PLATFORM=linux/amd64             image for x86 teammates
#   make build DOCKERFILE=docker/Dockerfile.orfs  full image, adds OpenROAD PnR
# ===========================================================================

IMAGE      ?= astra:latest
DOCKERFILE ?= docker/Dockerfile
PDKS       ?= nangate45
DESIGN     ?= mac_chain
DOCKER     ?= docker

# Native by default. Set PLATFORM to cross-build (e.g. for an x86 teammate).
ifdef PLATFORM
PLATFORM_FLAG = --platform $(PLATFORM)
endif

RUN_FLAGS = --rm -it $(PLATFORM_FLAG) -v "$(CURDIR)":/work -w /work

.PHONY: build shell doctor run syn pnr list save clean-runs help

build:
	$(DOCKER) buildx build $(PLATFORM_FLAG) --build-arg PDKS="$(PDKS)" \
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

clean:
	rm -rf runs/*
	find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete 2>/dev/null || true
	@echo "removed run outputs and python caches"

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
