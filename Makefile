VENV=.venv
PY=$(VENV)/bin/python3

.PHONY: all repair venv install lint format style dev-install

all: install
	$(PY) bot.py

repair: venv
	$(PY) scripts/repair_history.py

venv:
	@test -d $(VENV) || python3 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip

install: venv
	$(PY) -m pip install -r requirements.txt

dev-install: venv
	$(PY) -m pip install -r requirements-dev.txt

lint: dev-install
	$(VENV)/bin/ruff check .

format: dev-install
	$(VENV)/bin/ruff format .

style: format lint