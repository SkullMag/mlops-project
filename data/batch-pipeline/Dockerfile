FROM python:3.10-slim

WORKDIR /app

RUN pip install python-swiftclient keystoneauth1 python-keystoneclient

COPY batch.py .

CMD ["python", "batch.py"]
