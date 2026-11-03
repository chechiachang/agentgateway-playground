FROM python:3.14-alpine@sha256:eb2c62658b8d51bff0acc408504b76521d02030de56a8a3c1e830083e89b89d2
# The demo needs only stdlib; omit pip and its unused vendored dependencies.
RUN rm -rf /usr/local/lib/python3.14/site-packages/pip* /usr/local/bin/pip*
USER 65532:65532
CMD ["python", "-B", "/demo/demo-server.py"]
