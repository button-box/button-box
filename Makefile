.PHONY: test syntax scan check

test:
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v

syntax:
	PYTHONPYCACHEPREFIX=/tmp/message-box-public-candidate-pycache python3 -m compileall -q src tests
	for file in scripts/*.sh scripts/install/*.sh; do sh -n "$$file"; done
	sh -n scripts/messageboxctl
	bash -n src/syncloop.sh

scan:
	command -v gitleaks >/dev/null
	gitleaks detect --source . --no-git --redact=100

check: syntax test
