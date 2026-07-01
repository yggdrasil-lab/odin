FROM nousresearch/hermes-agent:latest

USER root

# Install GitHub CLI (gh)
RUN apt-get update && apt-get install -y --no-install-recommends wget ca-certificates nginx \
    && mkdir -p -m 0755 /etc/apt/keyrings \
    && wget -qO- https://cli.github.com/packages/githubcli-archive-keyring.gpg | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update \
    && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# Install Chromium + system dependencies for browser automation
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/hermes/.playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 \
    libcairo2 libcups2t64 libdbus-1-3 libdrm2 libgbm1 libglib2.0-0t64 \
    libnspr4 libnss3 libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 \
    libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2 xvfb \
    fonts-noto-color-emoji fonts-unifont libfontconfig1 libfreetype6 \
    xfonts-scalable fonts-liberation fonts-ipafont-gothic fonts-wqy-zenhei \
    fonts-tlwg-loma-otf fonts-freefont-ttf \
    && rm -rf /var/lib/apt/lists/* \
    && cd /opt/hermes && npx playwright install chromium \
    && chmod -R a+rX /opt/hermes/.playwright

# Install mnemosyne-memory in the virtual environment
RUN if [ ! -x "/opt/hermes/.venv/bin/pip" ]; then \
      echo "Installing pip inside virtualenv..." && \
      /opt/hermes/.venv/bin/python -c "import urllib.request, ssl; open('get-pip.py', 'wb').write(urllib.request.urlopen('https://bootstrap.pypa.io/get-pip.py', context=ssl._create_unverified_context()).read())" && \
      /opt/hermes/.venv/bin/python get-pip.py && \
      rm get-pip.py; \
    fi && \
    /opt/hermes/.venv/bin/pip install --no-cache-dir mnemosyne-memory[embeddings,mcp] sqlite-vec

# Copy and setup entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && chown -R 1000:1000 /opt/hermes

USER 1000:1000


