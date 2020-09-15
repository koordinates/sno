#!/bin/bash
set -ex

source /root/.bashrc

PYENV_PREFIX=$(pyenv prefix)

{ echo ">> Configuring vendor dependencies..."; } 2> /dev/null
tar xvzf vendor/dist/vendor-Linux.tar.gz -C /tmp wheelhouse
rm -rf vendor/dist/env/
tar xzf vendor/dist/vendor-Linux.tar.gz -C vendor/dist/ env
tar xzf vendor/dist/vendor-Linux.tar.gz -C "$PYENV_PREFIX" --strip-components=1 env/lib/

# get the Rtree installer working successfully
export SPATIALINDEX_C_LIBRARY="$PYENV_PREFIX/lib/libspatialindex_c.so"

pip install --no-deps --ignore-installed -r requirements/main.txt
pip install --no-deps \
    /tmp/wheelhouse/*.whl

{ echo ">> Downgrading PyInstaller (https://github.com/pyinstaller/pyinstaller/issues/4674) ..."; } 2> /dev/null
pip install "pyinstaller==3.5.*"

python setup.py install

{ echo ">> Pre-bundle Smoke Test ..."; } 2> /dev/null
pyenv exec sno --version

{ echo ">> Running PyInstaller ..."; } 2> /dev/null
pyinstaller \
    --clean -y \
    --workpath platforms/linux/build/ \
    --distpath platforms/linux/dist/ \
    sno.spec

{ echo ">> Post-bundle Smoke Test ..."; } 2> /dev/null
platforms/linux/dist/sno/sno_cli --version
