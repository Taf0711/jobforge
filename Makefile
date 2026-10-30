PY := .venv/bin/python
PIP := .venv/bin/pip

.PHONY: help bootstrap install setup app doctor serve test scan score tailor apply audit eval e2e smoke lock clean

help:
	@echo "JobForge"
	@echo "  make bootstrap   create .venv, install deps, download Chromium"
	@echo "  make setup       first-run config, data dirs, browser"
	@echo "  make app         dashboard at http://127.0.0.1:8765"
	@echo "  make doctor      check paths, Typst, and browser"
	@echo "  make serve       dashboard only (no browser launch)"
	@echo "  make test        run pytest"
	@echo "  make scan        python -m jobforge scan (uses config.yml)"
	@echo "  make score       rescore all jobs"
	@echo "  make tailor JOB=42"
	@echo "  make apply JOB=42"
	@echo "  make audit       parity + security checks"
	@echo "  make eval        deterministic ATS adapter eval (JSON)"
	@echo "  make e2e         offline end-to-end smoke (JSON)"
	@echo "  make smoke       live, non-submitting ATS fill smoke (network)"
	@echo "  make lock        regenerate requirements.lock"
	@echo "  make clean       remove caches (keeps data/)"

bootstrap:
	@if command -v uv >/dev/null 2>&1; then uv venv --python 3.11 .venv; else python3.11 -m venv .venv; fi
	@if command -v uv >/dev/null 2>&1; then uv pip install --python $(PY) -r requirements.lock; else $(PIP) install --upgrade pip && $(PIP) install -r requirements.lock; fi
	.venv/bin/playwright install chromium

install:
	$(PIP) install -r requirements.lock
	.venv/bin/playwright install chromium

setup:
	$(PY) -m jobforge setup

app:
	$(PY) -m jobforge app

doctor:
	$(PY) -m jobforge doctor

serve:
	$(PY) -m jobforge serve

test:
	$(PY) -m pytest -q

scan:
	$(PY) -m jobforge scan

score:
	$(PY) -m jobforge score

tailor:
	$(PY) -m jobforge tailor --job $(JOB)

apply:
	$(PY) -m jobforge apply --job $(JOB)

audit:
	$(PY) -m jobforge audit

publish-npm:
	scripts/publish_npm.sh

eval:
	$(PY) -m jobforge.audit.adapter_eval --all --json

decisions:
	$(PY) -m jobforge decisions replay

decision-compare:
	$(PY) -m jobforge decisions compare --json

e2e:
	$(PY) scripts/e2e_smoke.py --json

smoke:
	$(PY) scripts/smoke_live_ats.py --json

lock:
	uv pip compile requirements.txt -o requirements.lock

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache
