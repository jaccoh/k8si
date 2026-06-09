# NOTE: The existing CI job in .gitea/workflows/ci.yml (docker-build) builds only the
# main k8si operator image. A separate CI job targeting this Dockerfile (k8si/ui/Dockerfile)
# would be needed to build and push ghcr.io/jaccoh/k8si-ui:latest.

FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py dashboard.html ./

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
