FROM docker.io/library/python@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && mkdir -p /data && chown 65532:65532 /data
COPY flaskr ./flaskr
COPY LICENSE.txt NOTICE.md ./
USER 65532:65532
EXPOSE 8000
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import os, urllib.request; from urllib.parse import urlsplit; r=urllib.request.Request('http://127.0.0.1:8000/health/ready', headers={'Host':urlsplit(os.environ['APP_BASE_URL']).netloc}); urllib.request.urlopen(r, timeout=2).close()"
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "30", "--graceful-timeout", "25", "--forwarded-allow-ips=", "--access-logfile", "/dev/null", "flaskr:create_app()"]
