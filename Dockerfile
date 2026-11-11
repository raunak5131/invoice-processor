FROM python:3.12-slim

# poppler-utils (pdftotext/pdftoppm) and tesseract-ocr are required for reading invoices
RUN apt-get update \
 && apt-get install -y --no-install-recommends poppler-utils tesseract-ocr \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PORT=8000 INVOICE_DB=/tmp/invoices.db INVOICE_UPLOADS=/tmp/uploads INVOICE_SESSIONS=/tmp/sessions
EXPOSE 8000

# One worker + threads: SQLite is the store and the stream is long-lived per request
CMD ["sh", "-c", "gunicorn -w 1 --threads 8 --timeout 120 -b 0.0.0.0:${PORT} app:app"]
