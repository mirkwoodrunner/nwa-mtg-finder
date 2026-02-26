# Use Microsoft's official Playwright image — Chromium + all system deps pre-installed
# This avoids the need for apt-get / sudo during the build
FROM mcr.microsoft.com/playwright/python:v1.50.0-jammy

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright browsers are already installed in the base image
# Just make sure chromium is linked correctly
RUN python -m playwright install chromium

# Copy app files
COPY app.py .
COPY static/ ./static/

# Render injects $PORT at runtime
ENV PORT=5000

EXPOSE $PORT

CMD gunicorn app:app --workers 1 --timeout 90 --bind 0.0.0.0:$PORT
