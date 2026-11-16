# syntax=docker/dockerfile:1.7
#
# evilflowers-render-service: the `evilflowers_render.book` worker and every tool it shells out to.
#   * recode_pdf (archive-pdf-tools, from uv.lock) + jbig2enc (built here: no Debian package) + jpegoptim
#   * veraPDF (PDF/A check of the archival master; Java)
#   * pandoc (upstream release, pinned) + potrace (the EPUB; potrace turns line art into SVG, as in the digitalizer's)
#   * OpenJPEG through Pillow's wheel (JP2 package, JPEG 2000 MRC layers): no Kakadu
# Debian trixie, as the digitalizer's studio image.

ARG PYTHON_IMAGE=python:3.12-slim-trixie

# -- jbig2enc ----------------------------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS jbig2
# The digitalizer builds jbig2enc's default branch; 0.32 is its newest tag.
ARG JBIG2ENC_REF=0.32
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates build-essential automake autoconf libtool \
      pkg-config libleptonica-dev \
 && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch ${JBIG2ENC_REF} https://github.com/agl/jbig2enc.git /src/jbig2enc \
 && cd /src/jbig2enc && ./autogen.sh && ./configure --prefix=/opt/jbig2 && make -j"$(nproc)" && make install

# -- veraPDF -----------------------------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS verapdf
ARG VERAPDF_VERSION=1.30.2
ARG VERAPDF_SHA256=6cc6341cb1af644044054b81f00a6590a7918abb18f762243de115258bcad838
RUN apt-get update && apt-get install -y --no-install-recommends default-jre-headless curl unzip ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY docker/verapdf-auto-install.xml /tmp/auto-install.xml
RUN curl -fsSL -o /tmp/verapdf.zip \
      "https://software.verapdf.org/releases/${VERAPDF_VERSION%.*}/verapdf-greenfield-${VERAPDF_VERSION}-installer.zip" \
 && echo "${VERAPDF_SHA256}  /tmp/verapdf.zip" | sha256sum -c - \
 && unzip -q /tmp/verapdf.zip -d /tmp \
 && java -jar /tmp/verapdf-greenfield-${VERAPDF_VERSION}/verapdf-izpack-installer-${VERAPDF_VERSION}.jar \
      /tmp/auto-install.xml \
 && test -x /opt/verapdf/verapdf

# -- the virtual environment from uv.lock ------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS build
# git: uv fetches evilflowers-worker from its pinned commit ([tool.uv.sources]).
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --all-extras --no-install-project
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --all-extras --no-editable

# -- runtime -----------------------------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS base
# libleptonica-dev: jbig2's runtime library under a soname-stable package name (the t64 transition renamed the
# runtime packages; the digitalizer's image does the same).
RUN apt-get update \
 && apt-get install -y --no-install-recommends jpegoptim potrace default-jre-headless libleptonica-dev curl \
 && rm -rf /var/lib/apt/lists/*
# pandoc from upstream, not Debian: the reflow edition passes --math-method, which trixie's pandoc 3.1 does not know
# ("Unknown option --math-method", exit 6; found by this image's test stage, 2026-11-16). amd64 only, as the cluster.
ARG PANDOC_VERSION=3.13
ARG PANDOC_SHA256=7e3736f255090296ca5d1a14291a76b9b3c43cd385011b1621a4d0c16091a70f
RUN curl -fsSL -o /tmp/pandoc.deb \
      "https://github.com/jgm/pandoc/releases/download/${PANDOC_VERSION}/pandoc-${PANDOC_VERSION}-1-amd64.deb" \
 && echo "${PANDOC_SHA256}  /tmp/pandoc.deb" | sha256sum -c - \
 && dpkg -i /tmp/pandoc.deb && rm /tmp/pandoc.deb \
 && apt-get purge -y curl && apt-get autoremove -y
COPY --from=jbig2 /opt/jbig2 /usr/local
COPY --from=verapdf /opt/verapdf /opt/verapdf
WORKDIR /app
COPY --from=build /app/.venv /app/.venv

ARG IMAGE_TAG=dev
# No REMAP_SIGTERM: SIGTERM (scale-in, rollout) is a warm shutdown, and the kit's `shutdown` cancels and requeues the
# book after WORKER_SOFT_SHUTDOWN_TIMEOUT. HOME=/tmp: anything that insists on a cache dir (Java, pandoc) writes to the
# emptyDir, never to the read-only root.
ENV PATH=/app/.venv/bin:/opt/verapdf:$PATH \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    JAVA_TOOL_OPTIONS="-Djava.io.tmpdir=/tmp" \
    STORAGE_PATH=/mnt/data \
    IMAGE_TAG=${IMAGE_TAG}

RUN ldconfig \
 && command -v jbig2 && command -v jpegoptim && command -v pandoc && command -v potrace \
 && recode_pdf --version && verapdf --version \
 && python -c "from PIL import features; assert features.check('jpg_2000')" \
 && python -c "import evilflowers_render.tasks"

# -- test: the suite inside the image, every tool present (`docker build --target test .`; CI runs it) -----------------
FROM base AS test
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/uv
RUN uv pip install --python /app/.venv/bin/python "pytest>=8.3"
COPY pyproject.toml ./
COPY tests ./tests
RUN pytest -p no:cacheprovider -q \
 && pytest -p no:cacheprovider -q -m tools -rs | tee /tmp/tools.txt \
 && ! grep -q SKIPPED /tmp/tools.txt

# -- runtime (default target) ------------------------------------------------------------------------------------------
FROM base AS runtime
USER 65534:65534

# Prefork with one child: one book at a time per pod (KEDA scales pods, 0..N), Flower can inspect the worker while a
# book renders, and telemetry starts in the child. The k8s Deployment adds --hostname=render@<env>.%h.
CMD ["celery", "-A", "evilflowers_render.worker", "worker", "--loglevel=INFO", "-E", "--concurrency=1", \
     "--queues=evilflowers_render.standalone"]
