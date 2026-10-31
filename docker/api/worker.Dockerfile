ARG HAND_FIM_IMAGE=cuahsi/handfim:latest
FROM ${HAND_FIM_IMAGE}

USER root
COPY docker/api/requirements.txt /tmp/api-requirements.txt
RUN /opt/conda/bin/python -m pip install --no-cache-dir -r /tmp/api-requirements.txt
COPY src/fimbox_api /app/fimbox_api
COPY src/schemas /app/schemas
COPY docker/handfim/entry.py /home/entry.py
COPY docker/handfim/patches/runFIM.py /home/code/FIMServ/src/fimserve/runFIM.py
COPY docker/handfim/patches/streamFIM.py /home/code/FIMServ/src/fimserve/streamFIM.py
COPY docker/handfim/stream_storage.py /home/stream_storage.py
COPY docker/handfim/noaa_stream_rule.py /home/noaa_stream_rule.py
COPY docker/handfim/prepare_streaming_dataset.py /home/prepare_streaming_dataset.py
COPY docker/api/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && mkdir -p /home/data/inputs /home/output \
    && chown -R "${MAMBA_USER}" /home/data/inputs /home/output
USER ${MAMBA_USER}
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1 APP_ROLE=worker
ENTRYPOINT ["/entrypoint.sh"]
