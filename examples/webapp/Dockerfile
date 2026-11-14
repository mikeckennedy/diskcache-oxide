FROM python:3.14-slim

# A fixed, non-root user. The UID must never change between image versions: cache directories are
# created owner-only (0700), so a container running as another UID can't open them.
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --create-home app

# Create the mount point and give it to that user before switching to it. A new, empty named volume
# copies this directory's ownership the first time it's mounted.
RUN mkdir -p /var/lib/my-app && chown app:app /var/lib/my-app

WORKDIR /srv
COPY requirements.txt .
# Before a diskcache-oxide release is on PyPI, put its wheel in wheels/ and build with
# --build-arg LOCAL_WHEELS=1 (scripts/example-docker.sh does).
ARG LOCAL_WHEELS=0
COPY wheels/ /tmp/wheels/
RUN if [ "$LOCAL_WHEELS" = 1 ]; then pip install --no-cache-dir /tmp/wheels/*.whl; fi \
    && pip install --no-cache-dir -r requirements.txt \
    && rm -rf /tmp/wheels
COPY gunicorn.conf.py .
COPY app/ app/

USER app
ENV EXAMPLE_ROOT=/var/lib/my-app
EXPOSE 8000
CMD ["gunicorn", "-c", "gunicorn.conf.py"]
