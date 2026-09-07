FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_SYSTEM_PYTHON=1
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
COPY pyproject.toml README.md ./
COPY config ./config
COPY apps ./apps
COPY manage.py ./
RUN uv pip install --system -r pyproject.toml --extra dev
EXPOSE 8000
