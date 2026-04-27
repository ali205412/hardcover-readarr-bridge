FROM python:3.12-slim

WORKDIR /app
COPY bridge.py .

RUN mkdir -p /data

EXPOSE 9876

HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD python -c "from urllib.request import urlopen; urlopen('http://localhost:9876/health', timeout=5)" || exit 1

CMD ["python", "-u", "bridge.py"]
