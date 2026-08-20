.PHONY: test syntax lint lint-python lint-shell lint-frontend scan check

test:
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v

syntax:
	PYTHONPYCACHEPREFIX=/tmp/message-box-public-candidate-pycache python3 -m compileall -q src tests
	for file in scripts/*.sh scripts/install/*.sh; do sh -n "$$file"; done
	sh -n scripts/messageboxctl
	bash -n src/syncloop.sh

lint: lint-python lint-shell lint-frontend

lint-python:
	uvx --from ruff==0.16.3 ruff check --target-version=py313 --select=E4,E7,E9,F src tests

lint-shell:
	uvx --from shellcheck-py==0.11.0.1 shellcheck --exclude=SC1007,SC1091,SC2029 scripts/*.sh scripts/install/*.sh scripts/messageboxctl src/syncloop.sh

lint-frontend:
	bunx @biomejs/biome@2.5.9 lint --diagnostic-level=error src/onboarding/static src/dashboard_static

scan:
	command -v gitleaks >/dev/null
	gitleaks detect --source . --no-git --redact=100

check: syntax lint test
