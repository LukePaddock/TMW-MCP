# Pinned to bookworm (Debian 12) deliberately: Microsoft publishes msodbcsql18
# for Debian 12, and newer Debian releases lag behind in their repo.
FROM python:3.14-slim-bookworm AS builder

# Wheels are built here so the runtime image needs no compiler. pyodbc and
# cryptography ship manylinux wheels, but this keeps the build working if a
# pinned version ever lacks one for this interpreter.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential unixodbc-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements-docker.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements-docker.txt


FROM python:3.14-slim-bookworm AS runtime

# Microsoft ODBC Driver 18 for SQL Server. The Windows "SQL Server Native
# Client 11.0" driver does not exist on Linux, so TMW_DB_DRIVER must be
# "ODBC Driver 18 for SQL Server" in the container (compose sets this).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg ca-certificates \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && echo "deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
        > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc \
    && apt-get purge -y --auto-remove gnupg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /wheels /wheels
COPY requirements-docker.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements-docker.txt \
    && rm -rf /wheels requirements-docker.txt

# Run as a non-root user; nothing here needs to write to disk.
RUN useradd --create-home --shell /usr/sbin/nologin --uid 10001 tmw
WORKDIR /app
COPY --chown=tmw:tmw config.py tmw_db.py tmw_mcp.py healthcheck.py ./
USER tmw

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TMW_MCP_HOST=0.0.0.0 \
    TMW_MCP_PORT=8000

EXPOSE 8000

# A 401 proves both the ASGI app and the auth middleware are live; a plain
# port check would pass even if auth failed to attach.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "healthcheck.py"]

CMD ["python", "tmw_mcp.py", "--transport", "http"]
