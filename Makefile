# Thin aliases over the scripts; see scripts/verify.sh for what the gate runs.
.PHONY: verify lint-gaps

PHASE ?=

verify:
	@test -n "$(PHASE)" || { echo "usage: make verify PHASE=<1-15>"; exit 2; }
	bash scripts/verify.sh $(PHASE)

lint-gaps:
	python scripts/lint_no_gaps.py
