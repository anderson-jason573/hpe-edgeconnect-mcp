FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Streamable HTTP transport default port for FastMCP
EXPOSE 8001

ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8001

CMD ["python", "server.py"]
