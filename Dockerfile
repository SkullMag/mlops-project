FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy serving code
COPY app/ ./app/

# Create model directory (may be empty for initial bootstrap)
RUN mkdir -p ./model

# Copy model artifacts if present (populated by build-container-image workflow).
# The trailing slash + dot glob ensures COPY doesn't fail if model/ is empty.
COPY model/ ./model/

# Copy version file (created by build workflow; fallback baked into app)
COPY versions.tx[t] ./

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
