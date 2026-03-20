FROM python:3.13-slim

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir .

EXPOSE 9099/tcp
EXPOSE 9099/udp
EXPOSE 3000

CMD ["python", "-m", "mcp_aggregator.main"]
