FROM nousresearch/hermes-agent:latest

USER root

# Install mnemosyne-memory in the virtual environment
RUN if [ ! -x "/opt/hermes/.venv/bin/pip" ]; then \
      echo "Installing pip inside virtualenv..." && \
      /opt/hermes/.venv/bin/python -c "import urllib.request, ssl; open('get-pip.py', 'wb').write(urllib.request.urlopen('https://bootstrap.pypa.io/get-pip.py', context=ssl._create_unverified_context()).read())" && \
      /opt/hermes/.venv/bin/python get-pip.py && \
      rm get-pip.py; \
    fi && \
    /opt/hermes/.venv/bin/pip install --no-cache-dir mnemosyne-memory[all]

USER 1000:1000

