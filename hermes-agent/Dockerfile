FROM nousresearch/hermes-agent:latest

USER root

# Install mnemosyne-memory within the container's environment (using uv if available, falling back to pip)
RUN if command -v uv >/dev/null 2>&1; then \
      uv pip install --system mnemosyne-memory[all]; \
    else \
      pip install --no-cache-dir mnemosyne-memory[all]; \
    fi

USER 1000:1000
