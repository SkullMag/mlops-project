FROM python:3.10-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

RUN pip install boto3

COPY generate.py .

CMD ["python", "generate.py"]
