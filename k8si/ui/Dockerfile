FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py dashboard.html ./

ARG K8SI_VERSION=dev
ENV K8SI_VERSION=$K8SI_VERSION

# The UI needs no root: it only reads CRs and patches backup status.
# (The main k8si image intentionally stays root — restic must preserve
# file ownership in backup Jobs and the restore init container.)
USER 65532:65532

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
