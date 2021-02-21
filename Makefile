#
# Makefile to build and test py3Tivo
#
# Currently there are no tests and building consists only of running pylint
# over all the python source files.

# force the shell used to be bash in case we want to use 'set -o pipefail' so any failure
# in the pipe fails the command e.g.: set -o pipefail ; somecmd 2>&1 | tee $(LOG)
SHELL=/bin/bash

# Search path for targets and prerequisites
#VPATH = src

# Test if a variable has a value, callable from a recipe
# like $(call ndef,ENV)
ndef = $(if $(value $(1)),,$(error $(1) not set))

GIT_TAG_VERSION = $(shell git describe)
export GIT_TAG_VERSION


PYLINT = pylint

BUILD_LOG = logs/build.log

PLUGINS = music photo settings togo video
SOURCES = $(wildcard *.py) \
          $(foreach plugin,$(PLUGINS),$(wildcard plugins/$(plugin)/*.py))

# Pattern rules
# Lint errors shouldn't stop the build
%.lint : %.py
	$(PYLINT) $< > $@ || true

.DEFAULT_GOAL := help
.DELETE_ON_ERROR :
.PHONY : build lint doc test clean clean-build clean-doc outdated upgrade-deps upgrade-pip help

init : install ## run install; intended for initializing a fresh repo clone

install : VER ?= 3.9
install : ## create python3 virtual env, install requirements (define VER for other than python3)
	@python$(VER) -m venv venv
	@ln -s venv/bin/activate activate
	@source activate                            ; \
	pip install --upgrade pip setuptools wheel  ; \
	pip install -r requirements.txt             ;

run : ## Run pytivo, ctrl-c to exit
	$(call ndef,VIRTUAL_ENV)
	python pyTivo.py

build : lint

doc :

test :

clean : clean-build ## remove all build artifacts

clean-build :
	find . -type f -name '*.lint' -delete

lint : ## Run lint over all sources
lint : $(patsubst %.py,%.lint,$(SOURCES))

outdated : ## check for newer versions of required python packages
	$(call ndef,VIRTUAL_ENV)
	pip list --outdated

upgrade-deps : ## upgrade to the latest versions of required python packages
	$(call ndef,VIRTUAL_ENV)
	pip install --upgrade -r requirements.txt

upgrade-pip : ## upgrade pip and setuptools
	$(call ndef,VIRTUAL_ENV)
	pip install --upgrade pip setuptools wheel

## Help documentation à la https://marmelab.com/blog/2016/02/29/auto-documented-makefile.html
## if you want the help sorted rather than in the order of occurrence, pipe the grep to sort and pipe that to awk
help :
	@echo ""                                                                   ; \
	echo "Useful targets in this riff-infrastructure Makefile:"                ; \
	(grep -E '^[a-zA-Z_-]+ ?:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = " ?:.*?## "}; {printf "\033[36m%-20s\033[0m : %s\n", $$1, $$2}') ; \
	echo ""                                                                    ; \
	echo "If VIRTUAL_ENV needs to be set for a target, run '. activate' first" ; \
	echo ""
