.DEFAULT_GOAL := all

PY_VERSION ?= 3.7
PY_ID ?= cp37-cp37m

ifeq ($(OS),Windows_NT)
	$(error "On Windows, run `nmake /f makefile.vc` instead.")
else
	PLATFORM := $(shell uname -s)
endif

# Python binaries like python, pip in the venv will take precedence.
VIRTUAL_ENV ?= venv
SHELL = /bin/bash  # required for PATH to work

export PREFIX ?= /usr/local

# Python dependencies via pip-compile
BASE_PIP_COMPILE_CMD = CUSTOM_COMPILE_COMMAND="make py-requirements" pip-compile -v --annotate --no-index --no-emit-trusted-host --upgrade --allow-unsafe
PIP_COMPILE_CMD ?= $(BASE_PIP_COMPILE_CMD)
PY_REQS = requirements/main.txt requirements/test.txt requirements/dev.txt

# Native library dependencies
ifeq ($(PLATFORM),Darwin)
	LIBSUFFIX = dylib
	PY3 ?= $(realpath /Library/Frameworks/Python.framework/Versions/$(PY_VERSION)/bin/python$(PY_VERSION))
else
	LIBSUFFIX = so
endif
export PY3 := $(or $(PY3),python$(PY_VERSION))
PY_SITEPACKAGES = lib/python$(PY_VERSION)/site-packages

# use ccache if available
export PATH := $(abspath $(VIRTUAL_ENV)/bin):$(PATH)

# Create virtualenv
$(VIRTUAL_ENV):
	$(PY3) -m venv $@

.PHONY: py-venv-upgrade
py-venv-upgrade: | $(VIRTUAL_ENV)
	$(PY3) -m venv $(VIRTUAL_ENV) --upgrade
	rm requirements/.*.installed

# Python dependency compilation/resolution

.PHONY: py-requirements
py-requirements: $(py-install-tools)
	touch requirements/*.in
	@$(MAKE) $(PY_REQS) py-license-check

.PHONY: py-requirements-upgrade
py-requirements-upgrade: export PIP_COMPILE_CMD=$(BASE_PIP_COMPILE_CMD) --upgrade
py-requirements-upgrade: py-requirements

requirements/main.txt: requirements/requirements.in requirements/licenses.ini
requirements/test.txt: requirements/test.in requirements/main.txt
requirements/dev.txt: requirements/dev.in requirements/main.txt requirements/test.txt

requirements/%.txt:
	$(MAKE) $(py-install-tools) $(vendor-install)
	$(PIP_COMPILE_CMD) --output-file $@ $<
#   Comment out things we build
	sed -E -i.~bak -e 's/^(pygit2=)/\#\1/' $@
	sed -E -i.~bak -e 's/^(apsw=)/\#\1/' $@
	sed -E -i.~bak -e 's/^(psycopg2=)/\#\1/' $@
	$(RM) $@.~bak

# Python dependency license checking
.PHONY: py-license-check
py-license-check: py-deps $(py-install-tools) requirements/licenses.ini
	liccheck -l CAUTIOUS -s requirements/licenses.ini -r requirements/main.txt

# Python dependency installation

py-install-main = $(VIRTUAL_ENV)/.requirements.installed
py-install-dev = $(VIRTUAL_ENV)/.dev.installed
py-install-test = $(VIRTUAL_ENV)/.test.installed
py-install-tools = $(VIRTUAL_ENV)/.tools.installed

$(PY_REQS) $(py-install-main): export SPATIALINDEX_C_LIBRARY:=$(abspath $(VIRTUAL_ENV)/lib/libspatialindex_c.$(LIBSUFFIX))

$(py-install-main): requirements/main.txt $(vendor-install)
$(py-install-test): requirements/test.txt $(py-install-main)
$(py-install-dev): requirements/dev.txt $(py-install-main) $(py-install-test)

$(VIRTUAL_ENV)/.%.installed: | $(VIRTUAL_ENV)
	pip install --no-deps -r $<
	touch $@

$(py-install-tools): | $(VIRTUAL_ENV)
# Pin PyInstaller, upgrading isn't trivial

# For Linux we actually use PyInstaller 3.5: https://github.com/pyinstaller/pyinstaller/issues/4674
# See platforms/linux/pyinstaller.sh

# Fix PyInstaller 3.6 setup.cfg: https://github.com/pyinstaller/pyinstaller/issues/4609
ifeq ($(PLATFORM),Darwin)
	pip install 'macholib>=1.8'
endif
	# note: pip is pinned here, because https://github.com/dhatim/python-license-check/issues/40
	pip install \
		'pip~=20.0.0' \
		pip-tools \
		liccheck \
		pipdeptree \
		'pyinstaller==3.6.*' \
		$(WHEELTOOL)

	touch $@

.PHONY: py-tools
py-tools: $(py-install-tools)

# Vendor Dependencies

vendor-archive = vendor/dist/vendor-$(PLATFORM).tar.gz

$(vendor-archive):
	$(MAKE) -C vendor all

vendor-install := $(VIRTUAL_ENV)/.vendor-install

$(vendor-install): $(vendor-archive) | $(VIRTUAL_ENV)
	-$(RM) -r vendor/dist/wheelhouse
	tar xvzf $(vendor-archive) -C vendor/dist wheelhouse/
	pip install --force-reinstall --no-deps vendor/dist/wheelhouse/*.whl
	tar xzf $(vendor-archive) -C $(VIRTUAL_ENV) --strip-components=1 env/
	touch $@

.PHONY: vendor-install
vendor-install:
	-$(RM) $(vendor-install)
	$(MAKE) $(vendor-install)

# Install Python (just release) dependencies
.PHONY: py-deps
py-deps: $(vendor-install) $(py-install-main) | $(VIRTUAL_ENV)

# Install Python (development & release) py-deps
.PHONY: py-deps-dev
py-deps-dev: py-deps $(py-install-dev) $(py-install-tools)

# App code
sno-app-release = $(VIRTUAL_ENV)/$(PY_SITEPACKAGES)/sno
sno-app-dev = $(VIRTUAL_ENV)/$(PY_SITEPACKAGES)/sno.egg-link
sno-app-any = $(VIRTUAL_ENV)/bin/sno

$(sno-app-release): py-deps setup.py sno | $(VIRTUAL_ENV)
	-$(RM) dist/*
	python3 setup.py sdist
	pip install --force-reinstall --no-deps dist/*.tar.gz

$(sno-app-dev): py-deps-dev setup.py | $(VIRTUAL_ENV)
	pip install --force-reinstall --no-deps -e .

$(sno-app-any):
	$(MAKE) $(sno-app-release)

.PHONY: release
release: $(sno-app-release)

.PHONY: dev
dev: $(sno-app-dev)

# Top-level targets
.PHONY: all
all: dev

.PHONY: install
install: | $(sno-app-any)
	ln -sf $(realpath $(VIRTUAL_ENV)/bin/sno) $(PREFIX)/bin/sno

# Testing

.PHONY: test
test: $(py-install-test)
	pytest -v --cov-report term --cov-report html:coverage

ifeq ($(PLATFORM),Linux)
# (github actions only supports docker containers on linux)
ci-test: export SNO_POSTGRES_URL ?= postgresql://docker:docker@localhost:5432/gis
endif

.PHONY: ci-test
ci-test:
	CI=true pytest \
		-vv \
		--log-level=DEBUG \
		-p no:sugar \
		--cov-report term \
		--cov-report html:test-results/coverage/ \
		--junit-xml=test-results/junit.xml \
		--benchmark-enable \
		-p no:xdist

# Cleanup

.PHONY: clean
clean:
	$(RM) $(PREFIX)/bin/sno
	$(RM) -r $(VIRTUAL_ENV)

.PHONY: cleaner
cleaner: clean
	-$(MAKE) -C vendor clean clean-configure
	-$(MAKE) -C platforms clean

.PHONY: cleanest
cleanest: cleaner
	-$(MAKE) -C vendor cleaner

.PHONY: clean-docker-cache
clean-docker-cache:
	-docker builder prune --filter type=exec.cachemount --force
