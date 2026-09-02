FROM python:3.12-slim@sha256:e5c9fa26ffb76e11e0f054f30dc2523a2f9693f0c36c0cf1e39b27e152d899fc

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt ./adapter/requirements.txt
RUN pip install --disable-pip-version-check --no-cache-dir -r adapter/requirements.txt
COPY . ./adapter/

RUN groupadd --system --gid 65532 adapter \
    && useradd --system --uid 65532 --gid adapter --home-dir /nonexistent adapter

USER 65532:65532
EXPOSE 18080
ENTRYPOINT ["python", "-m", "adapter.cube_adapter"]
