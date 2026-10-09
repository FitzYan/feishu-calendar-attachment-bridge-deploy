FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOST=0.0.0.0 DATA_DIR=/app/data
WORKDIR /app
COPY requirements.lock.txt .
RUN pip install --no-cache-dir -r requirements.lock.txt && useradd --uid 10001 --create-home bridge
COPY service.py .
RUN mkdir /app/data && chown -R bridge:bridge /app
USER bridge
EXPOSE 8080
CMD ["python", "service.py", "serve"]
