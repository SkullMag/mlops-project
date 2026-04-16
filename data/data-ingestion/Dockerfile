FROM python:3.10-slim

WORKDIR /app

RUN pip install boto3 Pillow requests python-swiftclient keystoneauth1 python-keystoneclient

COPY ingest.py .

CMD ["python", "ingest.py"]
