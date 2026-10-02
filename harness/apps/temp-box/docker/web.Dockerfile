FROM docker.io/library/nginx@sha256:a8b39bd9cf0f83869a2162827a0caf6137ddf759d50a171451b335cecc87d236
ENV NGINX_ENVSUBST_FILTER="^(WAS_UPSTREAM|PUBLIC_HOST|PUBLIC_SCHEME|TRUSTED_PROXY_CIDR)$"
COPY docker/nginx/templates/ /etc/nginx/templates/
COPY --chmod=755 docker/nginx/19-validate-env.sh /docker-entrypoint.d/19-validate-env.sh
COPY flaskr/static/ /usr/share/nginx/html/static/
COPY LICENSE.txt NOTICE.md /usr/share/nginx/html/
EXPOSE 8080
STOPSIGNAL SIGQUIT
