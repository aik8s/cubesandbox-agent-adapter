FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN apt-get update \
    && apt-get upgrade --yes \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./adapter/requirements.txt
RUN pip install --disable-pip-version-check --no-cache-dir -r adapter/requirements.txt
COPY . ./adapter/

RUN groupadd --system --gid 65532 adapter \
    && useradd --system --uid 65532 --gid adapter --home-dir /nonexistent adapter \
    && install -d -o 65532 -g 65532 -m 0750 /var/log/cube-adapter

USER 65532:65532
EXPOSE 18080
ENTRYPOINT ["python", "-m", "adapter.cube_adapter"]
