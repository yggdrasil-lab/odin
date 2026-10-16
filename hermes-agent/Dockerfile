FROM nousresearch/hermes-agent:stable

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

# Install mnemosyne-memory in the virtual environment
RUN if [ ! -x "/opt/hermes/.venv/bin/pip" ]; then \
      echo "Installing pip inside virtualenv..." && \
      /opt/hermes/.venv/bin/python -c "import urllib.request, ssl; open('get-pip.py', 'wb').write(urllib.request.urlopen('https://bootstrap.pypa.io/get-pip.py', context=ssl._create_unverified_context()).read())" && \
      /opt/hermes/.venv/bin/python get-pip.py && \
      rm get-pip.py; \
    fi && \
    /opt/hermes/.venv/bin/pip install --no-cache-dir mnemosyne-memory[embeddings,mcp] sqlite-vec

# Install the Antigravity CLI (agy) used by the delegated-subagent route.
# The vendor installer is non-interactive and takes its target directory via
# --dir; it does NOT read AGY_INSTALL_DIR. `bash -s --` is required to pass the
# flag through the curl pipe.
RUN mkdir -p /opt/agy \
    && curl -fsSL --retry 3 https://antigravity.google/cli/install.sh \
      | bash -s -- --dir /opt/agy \
    && ln -sf /opt/agy/agy /usr/local/bin/agy \
    && chmod -R a+rX /opt/agy \
    && agy --version

# Install the Antigravity subscription model-provider plugin into the image layer.
# `hermes plugins install` writes to $HERMES_HOME/plugins, which is the persistent
# volume (/opt/data) at runtime, so anything installed there is shadowed at deploy
# time. Model-provider plugins are instead discovered unconditionally from the
# bundled image path (<repo>/plugins/model-providers/<name>/), so pin-install the
# plugin there. The sha is the one pinned by the official plugin catalog.
ARG ANTIGRAVITY_PLUGIN_SHA=d6c8ea5fe45da38abf2e26f30156123a1a3caab6
RUN git clone --quiet https://github.com/soyelmismo/hermes-antigravity-subscription /tmp/agy-plugin \
    && git -C /tmp/agy-plugin checkout --quiet "${ANTIGRAVITY_PLUGIN_SHA}" \
    && mkdir -p /opt/hermes/plugins/model-providers/antigravity-subscription-directsdk \
    && cp -R /tmp/agy-plugin/. /opt/hermes/plugins/model-providers/antigravity-subscription-directsdk/ \
    && rm -rf /tmp/agy-plugin \
      /opt/hermes/plugins/model-providers/antigravity-subscription-directsdk/.git \
    && chmod -R a+rX /opt/hermes/plugins/model-providers/antigravity-subscription-directsdk

# Patch Hermes so agy's "response was cut off ... exceeded the output token limit"
# is recognised as an output-cap error instead of a context overflow. Without it
# Hermes reports a misleading "server rejected the request as too large" notice.
# See patches/fix_agy_output_cap_detection.py for details. Fails the build if the
# upstream anchor moves so the fix is never silently dropped.
#
# Also patch the Antigravity plugin so worker-mode usage accounting calculates
# request-scoped token deltas instead of accumulating raw CLI session totals,
# which otherwise inflates prompt_tokens to >1.6M and forces an auto-reset.
# See patches/fix_agy_worker_usage_accumulation.py for details.
COPY patches/ /tmp/odin-patches/
RUN /opt/hermes/.venv/bin/python /tmp/odin-patches/fix_agy_output_cap_detection.py \
    && /opt/hermes/.venv/bin/python /tmp/odin-patches/fix_agy_worker_usage_accumulation.py \
    && rm -rf /tmp/odin-patches

# Copy and setup entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && chown -R 1000:1000 /opt/hermes

USER 1000:1000


