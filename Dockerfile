FROM python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app.py tools.py data_source.py index.html ./
COPY assets ./assets
ENV PYTHONUNBUFFERED=1 PORT=8080
EXPOSE 8080
CMD ["uv", "run", "--no-sync", "app.py"]
