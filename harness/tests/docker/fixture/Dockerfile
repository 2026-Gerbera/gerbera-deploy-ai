FROM python:3.13-alpine
WORKDIR /app
COPY server.py /app/server.py
COPY flaskr /app/flaskr
ARG VERSION
ARG READY=ok
ENV FIXTURE_READY=$READY
ENV FIXTURE_VERSION=$VERSION
EXPOSE 8000
ENTRYPOINT ["python", "/app/server.py"]
