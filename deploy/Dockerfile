FROM python:3.11-slim

# LibreOffice is used for PDF preview generation
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer \
        libreoffice-calc \
        libreoffice-impress \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8016

CMD ["python3", "-m", "gunicorn", \
     "--workers", "2", \
     "--threads", "4", \
     "--bind", "0.0.0.0:8016", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "app:app"]
