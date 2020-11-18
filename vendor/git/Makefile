GIT_VERSION = 2.29.2

SHELL = /bin/bash
export PREFIX ?= $(abspath env)

ifeq ($(OS),Windows_NT)
	PLATFORM := Windows
else
	PLATFORM := $(shell uname -s)
endif

GIT_ARCHIVE := git-$(GIT_VERSION).tar.gz

ifeq ($(PLATFORM),Darwin)
	CCACHE_PATH = /usr/local/opt/ccache/bin
	LIBSUFFIX = dylib
else ifeq ($(PLATFORM),Linux)
	CCACHE_PATH = /usr/lib/ccache
	CCACHE_PATH := $(or $(CCACHE_PATH),/usr/lib64/ccache)
	LIBSUFFIX = so
endif

# use ccache if available
export PATH := $(CCACHE_PATH):$(PREFIX)/bin:$(PATH)

export PKG_CONFIG_PATH := $(realpath $(PREFIX)/lib/pkgconfig)

ifeq ($(PLATFORM),Linux)
	CURL_PREFIX = $(PREFIX)
	libcurl := $(PREFIX)/lib/libcurl.$(LIBSUFFIX)
	libs += $(libcurl)
# something is broken in git's LDFLAGS handling
	export LDFLAGS := $(subst $$,$$$$,$(value LDFLAGS) -Wl,-rpath='$$ORIGIN/../../lib')
else
# on macOS, force to the Homebrew libcurl
	CURL_PREFIX=/usr/local/opt/curl
endif

CONFIG_FLAGS += CURL_CONFIG=$(CURL_PREFIX)/bin/curl-config
CONFIG_FLAGS += NO_GETTEXT=YesPlease
CONFIG_FLAGS += V=1
CONFIG_FLAGS += INSTALL_SYMLINKS=1

build-git := src/git
configure-git = src/config.status


# default target
.PHONY: all
all: $(build-git)

.PHONY: clean
clean:
	-$(RM) -r env
	-$(RM) -r src/git

.PHONY: cleaner
cleaner: clean
	-$(MAKE) -C src/ clean
	# git reconfigures itself to clean...
	$(MAKE) clean-configure

.PHONY: cleanest
cleanest:
	-$(RM) -r src/

.PHONY: clean-configure
clean-configure:
	-$(RM) $(configure-git)

#
# Download Archives
#

$(GIT_ARCHIVE):
# 	FIXME: kernel.org didn't renew their TLS certificate :/
# 	wget https://mirrors.edge.kernel.org/pub/software/scm/git/$@ -O $@
	wget https://mirror.aarnet.edu.au/pub/ftp.kernel.org/software/scm/git/$@ -O $@

.PHONY: archive
archive: $(GIT_ARCHIVE)

#
# Extract Archives
#

src: $(GIT_ARCHIVE)
	rm -rf $@
	mkdir -p $@
	@echo "Extracting $(GIT_ARCHIVE) ..."
	tar xzf $(GIT_ARCHIVE) --strip-components=1 -C $@


.PHONY: source
source: src

$(configure-git): export PKG_CONFIG_PATH=/dev/null
$(configure-git): | src $(libs)
	cd src/ && $(CONFIG_FLAGS) ./configure \
		--prefix=$(PREFIX) \
		--without-tcltk \
		--without-python \
		--with-curl=$(CURL_PREFIX)

.PHONY: configure
configure: clean-configure $(configure-git)

$(build-git): export PKG_CONFIG_PATH=/dev/null
$(build-git): $(configure-git)
	$(MAKE) -C src -j 2 $(CONFIG_FLAGS) all strip
	$(MAKE) print-lib-deps-$(PLATFORM)

#
# INSTALLATION
#

.PHONY: install
install: $(build-git)
	$(MAKE) -C src $(CONFIG_FLAGS) install

.PHONY: print-lib-deps
print-lib-deps: print-lib-deps-$(PLATFORM)

.PHONY: print-lib-deps-Darwin
print-lib-deps-Darwin:
	@echo -e "\nExamining library dependencies..."
	cd src/ && find . -perm -o=x -type f -name "git*" -exec otool -L {} \;

.PHONY: print-lib-deps-Linux
print-lib-deps-Linux:
	@echo -e "\nExamining library dependencies..."
	cd src/ && find . -perm -o=x -type f -name "git*" -print -exec sh -c "ldd {} && readelf -d {} | grep 'RPATH'" \;
