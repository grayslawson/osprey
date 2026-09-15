.PHONY: test typecheck compile validate compose check clean

PYTHON ?= python3

test:
	./cuckoo/test.sh
	./pyunifiwire/test.sh

typecheck:
	$(PYTHON) -m mypy --config-file cuckoo/mypy.ini cuckoo
	(cd pyunifiwire && $(PYTHON) -m mypy .)

compile:
	$(PYTHON) -m compileall -q cuckoo pyunifiwire/src

validate:
	./scripts/validate-repo.sh
	sh homeassistant/validate.sh

compose:
	command -v docker >/dev/null || { echo 'docker is required for compose validation' >&2; exit 1; }
	OSPREY_HOST=127.0.0.1 OSPREY_BIND=127.0.0.1 OSPREY_CAMERA_MAC=001122334455 OSPREY_IMAGE=ghcr.io/grayslawson/osprey:v0.0.0 docker compose -f compose.yaml config --quiet
	OSPREY_HOST=127.0.0.1 OSPREY_BIND=127.0.0.1 OSPREY_CAMERA_MAC=001122334455 OSPREY_IMAGE=ghcr.io/grayslawson/osprey:v0.0.0 docker compose -f compose.release.yaml config --quiet
	OSPREY_HOST=127.0.0.1 OSPREY_BIND=127.0.0.1 OSPREY_CAMERA_MAC=001122334455 OSPREY_IMAGE=ghcr.io/grayslawson/osprey:v0.0.0 docker compose -f compose.yaml -f compose.readonly.yaml config --quiet
	OSPREY_HOST=127.0.0.1 OSPREY_BIND=127.0.0.1 OSPREY_CAMERA_MAC=001122334455 OSPREY_IMAGE=ghcr.io/grayslawson/osprey:v0.0.0 docker compose -f compose.yaml -f compose.physical.yaml config --quiet

check: test compile validate

clean:
	rm -rf .pytest_cache .mypy_cache cuckoo/__pycache__ cuckoo/tests/__pycache__ pyunifiwire/.pytest_cache pyunifiwire/src/unifiwire/__pycache__ pyunifiwire/tests/__pycache__
