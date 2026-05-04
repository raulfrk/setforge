# Orchestrator for the dotdrop-managed dotfiles repo.
# Dotdrop manages config files under tracked/. This Makefile composes dotdrop
# with VSCode extension capture/restore, since extensions aren't config files
# and don't fit dotdrop's model cleanly.
#
# Common usage:
#   make help                       list targets
#   make compare PROFILE=vm-headless
#   make update  PROFILE=vm-headless
#   make install PROFILE=vm-headless

CFG      := --cfg $(HOME)/my-setup/config.yaml
DOTDROP  := uvx dotdrop $(CFG)
PROFILE ?=

LOCAL_FILES := \
	$(HOME)/.claude/header.md \
	$(HOME)/.claude/additional-content.md

EXT_FILE := vscode-extensions/$(PROFILE).txt

.PHONY: help compare capture update install install-ext sync require-profile

help: ## Show this help
	@awk 'BEGIN{FS=":.*## "} /^[a-z][a-z-]*:.*## / {printf "  %-14s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

require-profile:
	@if [ -z "$(PROFILE)" ]; then \
		echo "ERROR: PROFILE not set. e.g. make $(MAKECMDGOALS) PROFILE=vm-headless"; \
		exit 2; \
	fi

compare: require-profile ## Show drift between live files and tracked/
	$(DOTDROP) compare -p $(PROFILE)

capture: require-profile ## Snapshot installed VSCode extensions to vscode-extensions/<profile>.txt
	@command -v code >/dev/null || { echo "ERROR: 'code' not on PATH (open a VSCode terminal or install code-cli)"; exit 1; }
	@mkdir -p vscode-extensions
	code --list-extensions 2>/dev/null | grep -E '^[a-z0-9][a-z0-9-]*\.[a-z0-9][a-z0-9-]*$$' > $(EXT_FILE)
	@echo "Captured $$(wc -l < $(EXT_FILE)) extension(s) to $(EXT_FILE)"

update: require-profile ## Capture extensions (if code is available) + sync live -> tracked
	@if command -v code >/dev/null; then \
		$(MAKE) capture PROFILE=$(PROFILE); \
	else \
		echo "Note: 'code' not on PATH; skipping extension capture"; \
	fi
	$(DOTDROP) update -p $(PROFILE)

install-ext: require-profile ## Install extensions from vscode-extensions/<profile>.txt
	@command -v code >/dev/null || { echo "ERROR: 'code' not on PATH"; exit 1; }
	@if [ ! -s $(EXT_FILE) ]; then echo "ERROR: $(EXT_FILE) is empty or missing"; exit 1; fi
	xargs -L1 code --install-extension < $(EXT_FILE)

bootstrap-local:
	@for f in $(LOCAL_FILES); do \
		[ -f "$$f" ] || { mkdir -p "$$(dirname "$$f")"; touch "$$f"; echo "created stub: $$f"; }; \
	done

install: require-profile ## Deploy tracked -> live + install extensions
	$(DOTDROP) install -p $(PROFILE)
	$(MAKE) bootstrap-local
	@if command -v code >/dev/null && [ -s $(EXT_FILE) ]; then \
		$(MAKE) install-ext PROFILE=$(PROFILE); \
	else \
		echo "Note: skipping extensions ('code' missing or $(EXT_FILE) empty)"; \
	fi

sync: update ## Alias for update
