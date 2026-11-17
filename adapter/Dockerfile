FROM python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913

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
