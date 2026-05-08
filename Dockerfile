# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.12-slim

FROM python:${PYTHON_VERSION} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt


FROM python:${PYTHON_VERSION} AS runtime

ARG GIT_SHA=unknown

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    TZ=Europe/London

RUN apt-get update \
 && apt-get install -y --no-install-recommends tini ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 1001 tapestry \
 && useradd  --system --uid 1001 --gid 1001 --home /app --shell /bin/bash tapestry

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=tapestry:tapestry config.py scraper.py build_summary.py run_all.sh /app/
COPY --chown=tapestry:tapestry uploader/ /app/uploader/
COPY --chown=tapestry:tapestry config.example.toml .env.example /app/

RUN echo "${GIT_SHA}" > /app/.git-sha \
 && chown tapestry:tapestry /app/.git-sha \
 && chmod +x /app/run_all.sh \
 && mkdir -p /app/data \
 && chown tapestry:tapestry /app/data

LABEL org.opencontainers.image.source="https://github.com/albinati/tapestry-photo-scrapper" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.licenses="MIT"

USER tapestry

# Default invocation runs the full pipeline once and exits. This is a
# batch job, not a long-running service — there's no idle state to keep.
# Trigger from the host:
#   docker compose run --rm tapestry
# Override the command for one-offs:
#   docker compose run --rm tapestry python3 scraper.py
#   docker compose run --rm tapestry bash    # ad-hoc shell
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["bash", "/app/run_all.sh"]
