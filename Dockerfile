FROM python:3.12-slim

WORKDIR /app

# Install Python deps (cached layer — only rebuilds when requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project code
COPY polymarket/ polymarket/
COPY scripts/ scripts/

# Create runtime data dirs
RUN mkdir -p data/smart_wallets_history data/live_paper

# Copy wallet pool (rebuild with: docker exec bot python -m scripts.refresh_smart_wallets)
COPY data/smart_wallets_latest.json data/smart_wallets_latest.json

# The .env file is mounted at runtime via docker-compose (not baked into image)

# Default entrypoint: the trading bot
# Override CMD via docker-compose or docker run to change mode/flags
ENTRYPOINT ["python", "-u", "-m", "scripts.live_bot_ws"]
CMD ["--duration-min", "1440", "--max-cycles", "0"]
