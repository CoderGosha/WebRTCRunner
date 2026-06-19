FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=UTC

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates python3 \
    && rm -rf /var/lib/apt/lists/*

COPY app /app
WORKDIR /app

RUN chmod +x whitelist-bypass-cli-linux-x64/headless-* run_headless.py

CMD ["python3", "run_headless.py"]
