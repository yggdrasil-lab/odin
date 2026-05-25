FROM nousresearch/hermes-agent:latest

USER root

# Install mnemosyne-memory in the virtual environment or fallback to system python using uv/pip
RUN if [ -d "/opt/hermes/.venv" ]; then \
      /opt/hermes/.venv/bin/pip install --no-cache-dir mnemosyne-memory[all]; \
    elif [ -d "/app/venv" ]; then \
      /app/venv/bin/pip install --no-cache-dir mnemosyne-memory[all]; \
    elif [ -d "/app/.venv" ]; then \
      /app/.venv/bin/pip install --no-cache-dir mnemosyne-memory[all]; \
    elif [ -d "/venv" ]; then \
      /venv/bin/pip install --no-cache-dir mnemosyne-memory[all]; \
    elif command -v uv >/dev/null 2>&1; then \
      uv pip install --system --break-system-packages mnemosyne-memory[all] || \
      uv pip install mnemosyne-memory[all]; \
    elif command -v pip3 >/dev/null 2>&1; then \
      pip3 install --no-cache-dir --break-system-packages mnemosyne-memory[all] || \
      pip3 install --no-cache-dir mnemosyne-memory[all]; \
    else \
      pip install --no-cache-dir --break-system-packages mnemosyne-memory[all] || \
      pip install --no-cache-dir mnemosyne-memory[all]; \
    fi

USER 1000:1000

