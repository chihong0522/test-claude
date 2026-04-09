FROM python:3.11-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY pyproject.toml .
RUN pip install --no-cache-dir \
    httpx fastapi "uvicorn[standard]" "sqlalchemy>=2.0" alembic \
    aiosqlite asyncpg "pydantic>=2.0" pydantic-settings "apscheduler>=3.10" \
    numpy jinja2 python-dotenv

# Copy application
COPY polymarket/ polymarket/
COPY scripts/ scripts/

EXPOSE 8000

CMD ["uvicorn", "polymarket.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
