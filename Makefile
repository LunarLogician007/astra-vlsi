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

clean-runs:
	rm -rf runs/*

help:
	@grep -E '^#   ' $(MAKEFILE_LIST) | sed 's/^#   //'
