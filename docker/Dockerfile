# One image for every Python service of PdM-Agent (build plan T35). Each compose service runs it
# with its own command (compose.yaml); nothing service-specific is baked in.
#
#   docker compose build                                    # tags it pdm-agent:local
#   docker build -f docker/Dockerfile -t pdm-agent:local .  # the same, without compose
#
# The dataset, runs/, notebooks, .env and every key stay out of the image (.dockerignore). The
# replayer gets the dataset as a read-only mount; secrets arrive from .env at run time.
#
# NFR-05: pin the base image by digest on the demo host, e.g.
#   docker compose build --build-arg PYTHON_IMAGE=python:3.11.13-slim-bookworm@sha256:<digest>
# and record the digest of the built image (docs/RUNBOOK.md). The tag below was chosen where no
# registry could be reached, so no digest could be looked up.
ARG PYTHON_IMAGE=python:3.11.13-slim-bookworm
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so that a change to the code does not reinstall them. The dashboard's
# requirements (streamlit) come in too: one image serves every service.
COPY requirements.txt ./requirements.txt
COPY services/dashboard/requirements.txt ./services/dashboard/requirements.txt
RUN pip install -r requirements.txt -r services/dashboard/requirements.txt

# The shared library, pdm_common, as every service imports it.
COPY lib ./lib
RUN pip install ./lib

COPY . .

# An unprivileged user. The code stays owned by root, so a service cannot change it; runs/ is the
# one place the services write (quarantine, reconciliation, replay manifests, candidate requests),
# and compose mounts a volume there, which takes this owner on first use.
RUN useradd --create-home --uid 10001 --user-group pdm \
    && mkdir -p /app/runs \
    && chown pdm:pdm /app/runs
USER pdm

CMD ["python", "-c", "print('pdm-agent image: each compose service gives its own command (compose.yaml)')"]
