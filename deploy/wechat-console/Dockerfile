FROM python:3.12-slim
ARG CANDIDATE_REVISION
LABEL org.opencontainers.image.revision=$CANDIDATE_REVISION
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home console
USER 10001:10001
ENTRYPOINT ["python", "-m", "cf_agent_gateway.management_console.runtime"]
CMD ["serve", "--runtime", "/runtime", "--mode", "read_only", "--host", "0.0.0.0", "--port", "8080"]
