FROM python:3.12-slim

# Create non-root user
RUN groupadd -r agent && useradd -r -g agent -d /home/agent -s /bin/bash agent

# Install Claude SDK (httpx is a transitive dependency)
RUN pip install --no-cache-dir anthropic

# Create workspace
RUN mkdir -p /workspace && chown agent:agent /workspace

# Copy agent script
COPY agent.py /app/agent.py
RUN chown agent:agent /app/agent.py

USER agent
WORKDIR /workspace

ENTRYPOINT ["python", "/app/agent.py"]
