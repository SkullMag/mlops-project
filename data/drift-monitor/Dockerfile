FROM python:3.10-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
RUN pip install boto3 pillow numpy
COPY drift_monitor.py .
CMD ["python", "drift_monitor.py"]
