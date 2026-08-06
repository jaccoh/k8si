FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py dashboard.html ./

ARG K8SI_VERSION=dev
ENV K8SI_VERSION=$K8SI_VERSION

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
