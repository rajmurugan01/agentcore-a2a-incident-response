# AgentCore Runtime requires linux/arm64 -- built via DockerImageAsset(platform: LINUX_ARM64) in cdk/lib/stack.ts.
FROM --platform=linux/arm64 python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py cloudwatch_tools.py ./

# AgentCore Runtime's A2A-protocol containers are expected to listen on 8080.
ENV PORT=8080
EXPOSE 8080

CMD ["python", "agent.py"]
