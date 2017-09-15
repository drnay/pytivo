#
# Makefile to build and test py3Tivo
#
# Currently there are no tests and building consists only of running pylint
# over all the python source files.
# Running `make`, `make all`, `make build` and `make lint` all do the same
# thing, run pylint to create .lint files for any python sources modified
# since the associated .lint file was created.
#
# The outdated and upgrade-deps targets exist to make it easier to run and
# remember how to do those pip tasks.
#
# Other targets are empty placeholders for possible future needs.

# Search path for targets and prerequisites
#VPATH = src

APP_BUILD = $(shell git describe)

PYLINT = pylint

BUILD_LOG = logs/build.log

PLUGINS = music photo settings togo video
SOURCES = $(wildcard *.py) \
          $(foreach plugin,$(PLUGINS),$(wildcard plugins/$(plugin)/*.py))

.DELETE_ON_ERROR :
.PHONY : all build lint doc test clean clean-build clean-doc release outdated upgrade-deps

all : build

build : lint

doc :

test :

clean : clean-build

clean-build :
	-rm -r *.lint

lint : $(patsubst %.py,%.lint,$(SOURCES))

outdated:
	pip list --outdated

upgrade-deps: 
	pip install --upgrade -r requirements.txt

# Lint errors shouldn't stop the build
%.lint : %.py
	$(PYLINT) $< > $@ || true
