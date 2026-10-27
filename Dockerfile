# Minimal image — the app is Python stdlib only, no pip deps.
FROM python:3.12-slim

WORKDIR /app
COPY server.py qrcodegen.py ./
COPY public ./public
COPY deploy/docker-entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# data/, captures/ and models/ are bind-mounted volumes (persistent — never baked in).
ENV INVENTORY_HOST=0.0.0.0 INVENTORY_PORT=8770
EXPOSE 8770

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
