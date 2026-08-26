FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cloud Run sets $PORT (default 8080) and routes traffic to it; dashboard_server.py
# reads $PORT itself, so no need to hardcode it here.
CMD ["python", "dashboard_server.py"]
