FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

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
