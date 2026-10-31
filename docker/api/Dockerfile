FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY docker/api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -c "import rasterio; import rio_tiler"
COPY src/fimbox_api /app/fimbox_api
COPY src/schemas /app/schemas
COPY test-data/*.json /app/test-data/
COPY docker/api/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && useradd --system appuser
USER appuser
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 APP_ROLE=api PORT=8000 EXAMPLES_DIR=/app/test-data
EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
