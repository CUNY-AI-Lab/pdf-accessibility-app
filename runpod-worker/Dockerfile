FROM runpod/base:0.6.3-cuda11.8.0

# Use Python 3.11 (included in runpod/base)
RUN ln -sf $(which python3.11) /usr/local/bin/python && \
    ln -sf $(which python3.11) /usr/local/bin/python3

# Install dependencies
COPY requirements.txt /requirements.txt
RUN uv pip install --upgrade -r /requirements.txt hf_transfer --no-cache-dir --system

# Preload Docling models at build time so they're baked into the image.
# This eliminates model downloads at runtime and speeds up cold starts.
COPY preload_models.py /preload_models.py
RUN python /preload_models.py

# Add the handler
COPY handler.py /handler.py

CMD ["python", "-u", "/handler.py"]
