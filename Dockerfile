FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    git \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir ".[all]"

RUN mkdir -p /data /configs /workspace

ENV ELASTICITY_DATA_DIR=/data
ENV ELASTICITY_SESSION_DB=/data/sessions.db
ENV ELASTICITY_CHAT_LOG_FILE=/data/chat.log

EXPOSE 8080

CMD ["elasticity", "web", "--host", "0.0.0.0", "--port", "8080", "--config-dir", "/configs"]
