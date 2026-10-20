# AgentCore Runtime requires linux/arm64 -- built via DockerImageAsset(platform: LINUX_ARM64) in cdk/lib/stack.ts.
FROM --platform=linux/arm64 python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py cloudwatch_tools.py ./

# AgentCore Runtime's A2A ProtocolConfiguration proxies the container on port 9000 at
# the root path (confirmed against AWS's A2A devguide) -- NOT 8080/invocations, which is
# the plain-HTTP/MCP protocol convention. See agent.py for the full note.
ENV PORT=9000
EXPOSE 9000

CMD ["python", "agent.py"]
