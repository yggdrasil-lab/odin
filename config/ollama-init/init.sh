#!/bin/sh
set -e

# Start ollama in background
/bin/ollama serve &

# Wait for ollama to be responsive
echo "Waiting for Ollama to start..."
until /bin/ollama list > /dev/null 2>&1; do
  sleep 1
done
echo "Ollama started."

# Ensure environment variables are set
if [ -z "$OLLAMA_BASE_MODEL" ] || [ -z "$OLLAMA_MODEL_NAME" ] || [ -z "$HERMES_CONTEXT_LENGTH" ]; then
  echo "Error: OLLAMA_BASE_MODEL, OLLAMA_MODEL_NAME and HERMES_CONTEXT_LENGTH environment variables must be set." >&2
  exit 1
fi

CURRENT_SPEC="base_model: ${OLLAMA_BASE_MODEL} | context_length: ${HERMES_CONTEXT_LENGTH} | threads: ${OLLAMA_NUM_THREADS:-auto}"
SPEC_FILE="/root/.ollama/muninn_build_spec.txt"

# Read saved spec
SAVED_SPEC=""
if [ -f "$SPEC_FILE" ]; then
  SAVED_SPEC=$(cat "$SPEC_FILE")
fi

# Check if model exists
MODEL_EXISTS=false
if /bin/ollama list | grep -q "${OLLAMA_MODEL_NAME}"; then
  MODEL_EXISTS=true
fi

if [ "$MODEL_EXISTS" = "false" ] || [ "$CURRENT_SPEC" != "$SAVED_SPEC" ]; then
  echo "Building/rebuilding custom model ${OLLAMA_MODEL_NAME}..."
  echo "Spec: $CURRENT_SPEC"
  
  # Generate Modelfile dynamically
  cat <<EOF > /tmp/Modelfile
FROM ${OLLAMA_BASE_MODEL}
PARAMETER num_ctx ${HERMES_CONTEXT_LENGTH}
EOF

  if [ -n "$OLLAMA_NUM_THREADS" ]; then
    echo "PARAMETER num_thread ${OLLAMA_NUM_THREADS}" >> /tmp/Modelfile
  fi

  /bin/ollama create "${OLLAMA_MODEL_NAME}" -f /tmp/Modelfile
  
  # Save the spec to persistent storage
  echo "$CURRENT_SPEC" > "$SPEC_FILE"
  echo "Custom model built successfully."
else
  echo "Custom model ${OLLAMA_MODEL_NAME} is up-to-date."
fi

# Keep container running and stream logs
wait
