# Odin: The Central Orchestration Stack

Odin is the central stack designed to host local AI models and agent services for the **Yggdrasil** home server ecosystem.

## Components
*   **Muninn LLM (Ollama):** Running `qwen2.5:14b-instruct-q8_0` with a 96k context window optimized for background processing on Gaia's CPU.
*   **Huginn Agent (Hermes Agent):** Nous Research Hermes Agent executing tools and maintaining vault state.
*   **Odin Web UI (Open-WebUI):** High-performance chat UI.

## Deployment
This stack is deployed in Docker Swarm mode and routed via Traefik.
To deploy/update:
```bash
# Set environment variables (e.g. DOMAIN_NAME, OBSIDIAN_VAULT_PATH)
docker stack deploy -c docker-compose.yml odin
```
