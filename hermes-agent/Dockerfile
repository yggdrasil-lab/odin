FROM nousresearch/hermes-agent:latest

USER root

# Install mnemosyne-memory in the virtual environment or fallback to system python with break-system-packages
RUN if [ -d "/app/venv" ]; then \
      /app/venv/bin/pip install --no-cache-dir mnemosyne-memory[all]; \
    elif [ -d "/app/.venv" ]; then \
      /app/.venv/bin/pip install --no-cache-dir mnemosyne-memory[all]; \
    elif [ -d "/venv" ]; then \
      /venv/bin/pip install --no-cache-dir mnemosyne-memory[all]; \
    else \
      pip install --no-cache-dir --break-system-packages mnemosyne-memory[all] || \
      pip install --no-cache-dir mnemosyne-memory[all]; \
    fi

USER 1000:1000
