FROM python:3.12-slim

# Create non-root user
RUN groupadd -r agent && useradd -r -g agent -d /home/agent -s /bin/bash agent

# Install the Anthropic + OpenAI SDKs. httpx2 is declared explicitly because
# agent.py imports it directly rather than relying on the Anthropic SDK's
# dependency tree; openai enables the optional OpenAI provider.
RUN pip install --no-cache-dir anthropic httpx2 openai

# Create workspace
RUN mkdir -p /workspace && chown agent:agent /workspace

# Copy agent script
COPY agent.py /app/agent.py
RUN chown agent:agent /app/agent.py

USER agent
WORKDIR /workspace

ENTRYPOINT ["python", "/app/agent.py"]
