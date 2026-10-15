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

# PDM_ROOT: pdm_common is installed into site-packages below, so it is told where the
# repository's schemas/, corpus/ and skills/ are (lib/pdm_common/paths.py).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PDM_ROOT=/app \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Extra trusted root certificates, for a network that inspects HTTPS (a company proxy re-signs
# every connection, and pip then fails with CERTIFICATE_VERIFY_FAILED). PEM files in docker/ca/
# (*.crt, git-ignored; docker/ca/README.md says how to export them) are added to the system bundle,
# and pip, requests, httpx and OpenSSL all read that bundle. With none there, nothing changes.
COPY docker/ca/ /tmp/extra-ca/
RUN cat /etc/ssl/certs/ca-certificates.crt $(find /tmp/extra-ca -name '*.crt') > /etc/ssl/certs/pdm-ca-bundle.crt \
    && rm -rf /tmp/extra-ca
ENV SSL_CERT_FILE=/etc/ssl/certs/pdm-ca-bundle.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/pdm-ca-bundle.crt \
    PIP_CERT=/etc/ssl/certs/pdm-ca-bundle.crt

# Dependencies first, so that a change to the code does not reinstall them. The dashboard's
# requirements (streamlit) come in too: one image serves every service.
COPY requirements.txt ./requirements.txt
COPY services/dashboard/requirements.txt ./services/dashboard/requirements.txt
RUN pip install -r requirements.txt -r services/dashboard/requirements.txt

# The shared library, pdm_common, as every service imports it.
# Files copied from a folder with owner-only permissions (a OneDrive folder gives -rw-------) would be
# unreadable to the pdm user below, and pip would install pdm_common with the same modes. a+rX makes
# everything readable (and folders listable) while root keeps ownership, so a service still cannot
# change its code.
COPY lib ./lib
RUN chmod -R a+rX lib && pip install ./lib

COPY . .

# An unprivileged user. The code stays owned by root, so a service cannot change it; runs/ is the
# one place the services write (quarantine, reconciliation, replay manifests, candidate requests),
# and compose mounts a volume there, which takes this owner on first use.
RUN chmod -R a+rX /app \
    && useradd --create-home --uid 10001 --user-group pdm \
    && mkdir -p /app/runs \
    && chown pdm:pdm /app/runs
USER pdm

CMD ["python", "-c", "print('pdm-agent image: each compose service gives its own command (compose.yaml)')"]
