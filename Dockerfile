FROM python:3.15.2-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 DATA_DIR=/data
WORKDIR /opt/app
RUN groupadd --gid 10001 mcm && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin mcm
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY app ./app
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--no-proxy-headers", "--no-server-header"]

FROM runtime AS test
USER root
COPY requirements-dev.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements-dev.txt
COPY tests ./tests
COPY scripts ./scripts
COPY pyproject.toml ./
USER 10001:10001
CMD ["python", "-m", "pytest", "-q", "-m", "not container", "-p", "no:cacheprovider"]

FROM runtime AS production
