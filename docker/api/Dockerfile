FROM python:3.12-slim

WORKDIR /app
COPY docker/api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/fimbox_api /app/fimbox_api
COPY src/schemas /app/schemas
COPY docker/api/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && useradd --system appuser
USER appuser
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 APP_ROLE=api PORT=8000
EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
