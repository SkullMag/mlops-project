FROM python:3.10-slim

WORKDIR /app

RUN pip install fastapi uvicorn python-swiftclient keystoneauth1 python-keystoneclient Pillow python-multipart

COPY feature_service.py .

CMD ["python", "feature_service.py"]
