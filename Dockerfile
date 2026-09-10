FROM python:3.14.7-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.10@sha256:2bb3ebca0a796a155094a27773d290c4b074572e6107f171d88d086682fd2500 /uv /bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.14.7-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6
RUN groupadd --gid 10001 research && useradd --uid 10001 --gid research --create-home research
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY app.py alembic.ini ./
COPY migrations ./migrations
COPY configs ./configs
COPY .streamlit/config.toml ./.streamlit/config.toml
COPY scripts/seed_demo.py ./scripts/seed_demo.py
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    XDG_CACHE_HOME=/tmp/research-cache
USER 10001:10001
EXPOSE 8501
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0"]
