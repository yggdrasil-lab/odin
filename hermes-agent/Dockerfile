FROM nousresearch/hermes-agent:latest

USER root

# Install GitHub CLI (gh)
RUN apt-get update && apt-get install -y --no-install-recommends wget ca-certificates \
    && mkdir -p -m 0755 /etc/apt/keyrings \
    && wget -qO- https://cli.github.com/packages/githubcli-archive-keyring.gpg | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update \
    && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# Install mnemosyne-memory in the virtual environment
RUN if [ ! -x "/opt/hermes/.venv/bin/pip" ]; then \
      echo "Installing pip inside virtualenv..." && \
      /opt/hermes/.venv/bin/python -c "import urllib.request, ssl; open('get-pip.py', 'wb').write(urllib.request.urlopen('https://bootstrap.pypa.io/get-pip.py', context=ssl._create_unverified_context()).read())" && \
      /opt/hermes/.venv/bin/python get-pip.py && \
      rm get-pip.py; \
    fi && \
    /opt/hermes/.venv/bin/pip install --no-cache-dir mnemosyne-memory[all] sqlite-vec

USER 1000:1000


