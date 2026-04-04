FROM public.ecr.aws/docker/library/python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ awscli && \
    rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# Install Python dependencies (rarely changes — cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    python -c "from strands import Agent; print('strands-agents OK')"

# Bundle code as fallback (used when DATA_BUCKET is not set)
COPY . .

# Entrypoint syncs latest code from S3, then starts uvicorn
COPY cloud/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
