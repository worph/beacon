FROM python:3.13-slim

WORKDIR /app
COPY mock-servers/mock-tasks/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sdk/python/ /app/sdk/python/
RUN pip install --no-cache-dir /app/sdk/python/
COPY mock-servers/mock-tasks/server.py .

EXPOSE 9099/tcp
EXPOSE 9099/udp

CMD ["python", "server.py"]
