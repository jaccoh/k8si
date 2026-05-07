FROM python:3.12-slim AS builder

RUN pip install uv
WORKDIR /build
COPY pyproject.toml .
COPY k8si/ k8si/
RUN uv pip install --system --no-cache .


FROM python:3.12-slim

# restic + openssh + sqlite3 for pre-backup hooks
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    sqlite3 \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG RESTIC_VERSION=0.18.1
RUN curl -fsSL "https://github.com/restic/restic/releases/download/v${RESTIC_VERSION}/restic_${RESTIC_VERSION}_linux_arm64.bz2" \
    | bunzip2 > /usr/local/bin/restic \
    && chmod +x /usr/local/bin/restic

COPY --from=builder /usr/local/lib/python3.12 /usr/local/lib/python3.12
COPY --from=builder /usr/local/bin/k8si /usr/local/bin/k8si

USER 65532:65532

ENTRYPOINT ["k8si"]
