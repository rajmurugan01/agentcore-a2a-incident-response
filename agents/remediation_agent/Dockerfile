# AgentCore Runtime requires linux/arm64 -- built via DockerImageAsset(platform: LINUX_ARM64) in cdk/lib/stack.ts.
FROM --platform=linux/arm64 python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py runbooks.json ./

# AgentCore Runtime's A2A ProtocolConfiguration proxies the container on port 9000 at
# the root path -- see monitoring_agent/Dockerfile for the full note.
ENV PORT=9000
EXPOSE 9000

CMD ["python", "agent.py"]
