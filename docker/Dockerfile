ARG BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BASE_IMAGE}

ARG SOURCE_COMMIT=unrecorded
LABEL org.opencontainers.image.source="https://github.com/Tangbohu09527/CF_agent-gateway" \
    org.opencontainers.image.revision="${SOURCE_COMMIT}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CF_AGENT_GATEWAY_ALEMBIC_CONFIG=/app/alembic.ini

WORKDIR /app

RUN groupadd --gid 10001 gateway \
    && useradd --uid 10001 --gid gateway --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin gateway

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir . \
    && python -m pip freeze --all > /app/python-packages.txt

COPY alembic.ini ./
COPY config ./config
RUN install -d -o gateway -g gateway -m 0750 \
        /app/data /var/lib/cf-agent-gateway /run/cf-agent-gateway \
    && chown -R gateway:gateway /app

USER 10001:10001
EXPOSE 8080

CMD ["python", "-m", "cf_agent_gateway.main"]
