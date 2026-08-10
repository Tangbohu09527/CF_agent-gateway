FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CF_AGENT_GATEWAY_ALEMBIC_CONFIG=/app/alembic.ini

WORKDIR /app

RUN groupadd --system gateway && useradd --system --gid gateway gateway

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir .

COPY alembic.ini ./
COPY config ./config
RUN mkdir -p /app/data /var/lib/cf-agent-gateway \
    && chown -R gateway:gateway /app /var/lib/cf-agent-gateway

USER gateway
EXPOSE 8080

CMD ["python", "-m", "cf_agent_gateway.main"]
