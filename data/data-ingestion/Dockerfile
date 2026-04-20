FROM python:3.10-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

RUN pip install boto3

COPY ingest.py .

CMD ["python", "ingest.py"]
