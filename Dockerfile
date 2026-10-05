FROM ghcr.io/astral-sh/uv:0.11.15 AS uv

FROM python:3.13-slim-bookworm AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.13-slim-bookworm AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium fonts-noto-core fonts-noto-color-emoji ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 shopping \
    && useradd --uid 1000 --gid 1000 --create-home shopping \
    && install -d -o shopping -g shopping -m 700 /app/.browser-profile /data
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BROWSER_USE_HEADLESS=true \
    ANONYMIZED_TELEMETRY=false \
    BROWSER_USE_CLOUD_SYNC=false \
    SHOPPING_DB=/data/shopping.db
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --chown=shopping:shopping shopping.py telegram_bot.py run_logging.py web_app.py ./
COPY --chown=shopping:shopping web/ ./web/
USER shopping
RUN python -c "from browser_use.browser.watchdogs.local_browser_watchdog import LocalBrowserWatchdog; assert LocalBrowserWatchdog._find_installed_browser_path() == '/usr/bin/chromium'"
CMD ["python", "telegram_bot.py"]
