# Dockerfile — for other users who prefer containerised deployment.
# Personal deployment uses systemd + venv via deploy-license-server.sh.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
EXPOSE 8001
CMD ["python", "main.py"]
