FROM public.ecr.aws/docker/library/node:22-bookworm-slim AS frontend
WORKDIR /build/web
COPY web/package*.json ./
COPY web/.npmrc ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM public.ecr.aws/docker/library/python:3.12-slim AS api
WORKDIR /app
ADD https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem /app/rds-ca.pem
COPY pyproject.toml README.md LICENSE requirements.lock ./
COPY src/ ./src/
COPY data/ ./data/
RUN pip install --no-cache-dir -c requirements.lock .
COPY --from=frontend /build/web/dist ./web/dist
RUN chmod 644 /app/rds-ca.pem && useradd --uid 10001 --create-home steward
USER steward
ENV STEWARD_SEED_DIR=/app/data/seed STEWARD_WEB_ROOT=/app/web/dist STEWARD_DATABASE_PATH=/home/steward/steward.db
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "steward.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
