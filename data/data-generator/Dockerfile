FROM python:3.10-slim

WORKDIR /app

RUN pip install python-swiftclient keystoneauth1 python-keystoneclient

COPY generate.py .

CMD ["python", "generate.py"]
