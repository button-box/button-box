.PHONY: test syntax lint lint-python lint-shell lint-frontend check

test:
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v

syntax:
	PYTHONPYCACHEPREFIX=/tmp/messagebox-pycache python3 -m compileall -q messagebox tests scripts/install/audio_config.py
	for file in scripts/*.sh scripts/commands/* scripts/dev/onboard.sh scripts/dev/hardware-test.sh scripts/dev/reprovision.sh scripts/install/*.sh; do sh -n "$$file"; done
	sh -n scripts/messageboxctl
	bash -n messagebox/syncloop.sh

lint: lint-python lint-shell lint-frontend

lint-python:
	uvx --from ruff==0.16.3 ruff check --target-version=py313 --select=E4,E7,E9,F messagebox tests scripts/install/audio_config.py

lint-shell:
	uvx --from shellcheck-py==0.11.0.1 shellcheck --exclude=SC1007,SC1091,SC2029 scripts/*.sh scripts/commands/* scripts/dev/onboard.sh scripts/dev/hardware-test.sh scripts/dev/reprovision.sh scripts/install/*.sh scripts/messageboxctl messagebox/syncloop.sh

lint-frontend:
	bunx @biomejs/biome@2.5.9 lint --diagnostic-level=error messagebox/onboarding/static

check: syntax lint test
