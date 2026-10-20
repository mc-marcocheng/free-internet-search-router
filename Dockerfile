FROM node:22-bookworm-slim AS reader-runtime
WORKDIR /runtime
COPY src/search_router/reader/node/package*.json ./
RUN npm ci --omit=dev --ignore-scripts && npm run install-browser

FROM python:3.12-slim-bookworm
WORKDIR /app
COPY --from=reader-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=reader-runtime /runtime/node_modules /opt/reader-node-modules
RUN node /opt/reader-node-modules/playwright-core/cli.js install-deps chromium
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev --no-editable
RUN runtime=$(/app/.venv/bin/python -c "from pathlib import Path; import search_router.reader; print(Path(search_router.reader.__file__).parent / 'node')") \
	&& mv /opt/reader-node-modules "$runtime/node_modules"
RUN useradd --uid 10001 --create-home router
RUN install -d -m 0700 -o router -g router /data
COPY --from=reader-runtime --chown=router:router /root/.cloakbrowser /home/router/.cloakbrowser
USER router
ENV ROUTER_HOST=0.0.0.0
ENV ROUTER_DATABASE=/data/config.sqlite3
ENV ROUTER_PORT=9092
EXPOSE 9092
CMD ["/app/.venv/bin/search-router"]