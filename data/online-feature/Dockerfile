FROM python:3.10-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

RUN pip install fastapi uvicorn boto3 Pillow python-multipart

COPY feature_service.py .

CMD ["python", "feature_service.py"]
