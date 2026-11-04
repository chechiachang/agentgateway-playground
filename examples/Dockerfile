FROM python:3.14-alpine@sha256:eb2c62658b8d51bff0acc408504b76521d02030de56a8a3c1e830083e89b89d2
# Install only the required PostgreSQL driver; runtime remains non-root.
RUN pip install --no-cache-dir --no-compile 'psycopg[binary]==3.2.10' \
    && rm -rf /usr/local/lib/python3.14/site-packages/pip* /usr/local/bin/pip*
USER 65532:65532
