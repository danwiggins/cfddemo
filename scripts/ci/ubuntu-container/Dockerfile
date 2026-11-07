# Reproduction image for the hosted `ci (ubuntu-latest)` job. It is a
# reproduction aid for Ubuntu-only test failures (headless Chrome font and
# layout differences, Linux-only tool behaviour); it is never the hosted gate.
# Run it through scripts/ci/ubuntu-container/run.sh.
#
# Mirrors .github/workflows/ci.yml: Ubuntu 24.04, uv 0.11.10 with Python 3.11,
# and the same pinned, checksummed samtools 1.21 and minimap2 2.28 releases.
# tests/test_ci_ubuntu_container_pins.py fails when the tool pins here and in
# ci.yml drift apart. Google Chrome is the current stable release, like the
# browser preinstalled on the hosted runner image.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Runtime packages, Google Chrome stable, and the aligner tools built exactly
# as ci.yml builds them. One layer so the build toolchain and apt lists are
# gone from the final image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl bzip2 git nodejs \
        fonts-liberation fonts-dejavu-core \
        build-essential zlib1g-dev libbz2-dev liblzma-dev libcurl4-openssl-dev \
    && curl -sSLf -o /tmp/chrome.deb \
        https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y --no-install-recommends /tmp/chrome.deb \
    && rm -f /tmp/chrome.deb /etc/apt/sources.list.d/google-chrome.list \
    && mkdir /tmp/tools && cd /tmp/tools \
    && curl -sSLf -o minimap2.tar.bz2 https://github.com/lh3/minimap2/releases/download/v2.28/minimap2-2.28_x64-linux.tar.bz2 \
    && echo "51f2cf0e486d0f9f88ace1aa58fdc56571382a676ea0889ae607301c60693377  minimap2.tar.bz2" | sha256sum -c - \
    && tar -xjf minimap2.tar.bz2 \
    && install -m 0755 minimap2-2.28_x64-linux/minimap2 /usr/local/bin/minimap2 \
    && curl -sSLf -o samtools.tar.bz2 https://github.com/samtools/samtools/releases/download/1.21/samtools-1.21.tar.bz2 \
    && echo "05724b083a6b6f0305fcae5243a056cc36cf826309c3cb9347a6b89ee3fc5ada  samtools.tar.bz2" | sha256sum -c - \
    && tar -xjf samtools.tar.bz2 \
    && cd samtools-1.21 \
    && ./configure --prefix=/usr/local --without-curses > /dev/null \
    && make -j"$(nproc)" > /dev/null \
    && make install > /dev/null \
    && cd / && rm -rf /tmp/tools \
    && apt-get purge -y --auto-remove \
        build-essential zlib1g-dev libbz2-dev liblzma-dev libcurl4-openssl-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && samtools --version | head -1 \
    && minimap2 --version \
    && google-chrome --version

# uv 0.11.10 with a managed Python 3.11, as actions/setup-uv provides in ci.yml.
RUN curl -LsSf https://astral.sh/uv/0.11.10/install.sh \
        | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh \
    && uv python install 3.11 \
    && uv cache clean \
    && uv --version

COPY chrome-wrapper.sh /usr/local/bin/chrome-wrapper.sh
RUN chmod 0755 /usr/local/bin/chrome-wrapper.sh

WORKDIR /work
