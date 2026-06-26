FROM python:3.13-slim AS builder

WORKDIR /build
COPY pyproject.toml .
COPY k8si/ k8si/
RUN pip install --no-cache-dir .


FROM python:3.13-slim

# restic + openssh
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    curl \
    bzip2 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG RESTIC_VERSION=0.18.1
ARG KOPIA_VERSION=0.15.0
ARG TARGETARCH
RUN curl -fsSL "https://github.com/restic/restic/releases/download/v${RESTIC_VERSION}/restic_${RESTIC_VERSION}_linux_${TARGETARCH}.bz2" \
    | bunzip2 > /usr/local/bin/restic \
    && chmod +x /usr/local/bin/restic \
    && if [ "$TARGETARCH" = "amd64" ]; then KOPIA_ARCH="x64"; else KOPIA_ARCH="arm64"; fi \
    && curl -fsSL "https://github.com/kopia/kopia/releases/download/v${KOPIA_VERSION}/kopia-${KOPIA_VERSION}-linux-${KOPIA_ARCH}.tar.gz" \
    | tar -xz --strip-components=1 -C /usr/local/bin "kopia-${KOPIA_VERSION}-linux-${KOPIA_ARCH}/kopia" \
    && chmod +x /usr/local/bin/kopia

COPY --from=builder /usr/local/lib/python3.13 /usr/local/lib/python3.13
COPY --from=builder /usr/local/bin/k8si /usr/local/bin/k8si

ENTRYPOINT ["k8si"]
