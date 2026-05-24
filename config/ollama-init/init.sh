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

# Check and build the custom model
if ! /bin/ollama list | grep -q "qwen2.5-muninn:latest"; then
  echo "Custom model qwen2.5-muninn:latest not found. Building it..."
  /bin/ollama create qwen2.5-muninn:latest -f /Modelfile
  echo "Custom model built successfully."
else
  echo "Custom model qwen2.5-muninn:latest already exists."
fi

# Keep container running and stream logs
wait
