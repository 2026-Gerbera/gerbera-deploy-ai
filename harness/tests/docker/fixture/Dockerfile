FROM python:3.13-alpine
WORKDIR /app
COPY server.py /app/server.py
COPY flaskr /app/flaskr
ARG VERSION
ENV FIXTURE_VERSION=$VERSION
EXPOSE 8000
ENTRYPOINT ["python", "/app/server.py"]
