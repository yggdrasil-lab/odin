FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Clone the dashboard source
RUN git clone https://github.com/wysie/mnemosyne-dashboard.git .

# Install mnemosyne-memory (since the dashboard core imports it)
RUN pip install --no-cache-dir --break-system-packages mnemosyne-memory[embeddings,mcp] sqlite-vec

# Bind sqlite-vec extension into dashboard_core
COPY patch_sqlite_vec.py /app/patch_sqlite_vec.py
RUN python /app/patch_sqlite_vec.py

ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["python", "-u", "server.py"]
