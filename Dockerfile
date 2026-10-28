FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock && useradd --create-home trickee
COPY trickee ./trickee
COPY fleet-console ./fleet-console
COPY driver-console ./driver-console
COPY scripts/backup.py ./scripts/backup.py
COPY scripts/purge_phone_data.py ./scripts/purge_phone_data.py
RUN mkdir /app/data && chown -R trickee:trickee /app/data
USER trickee
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=2)"
CMD ["uvicorn", "trickee.api:app", "--host", "0.0.0.0", "--port", "8000"]
