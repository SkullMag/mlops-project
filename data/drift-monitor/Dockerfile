FROM python:3.10-slim
WORKDIR /app
RUN pip install swiftclient pillow numpy
COPY drift_monitor.py .
CMD ["python", "drift_monitor.py"]
