# Dockerfile
# FastAPI Droplet Manager with control endpoints
FROM python:3.11-slim

WORKDIR /app

# Install runtime deps
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY main.py /app/

EXPOSE 8000

# Start FastAPI
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

