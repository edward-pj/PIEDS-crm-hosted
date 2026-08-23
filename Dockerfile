# One image, both apps.
#
# core_django and local_agent share `shared/` and are pinned to the same Python
# and the same dependency set, so building them twice buys nothing. The image
# ships both; docker-compose picks which one a container runs by choosing an
# entrypoint. The security split the README describes is enforced by which
# environment variables each container gets, not by which files it has:
# the agent container is never given DATABASE_URL.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

# postgresql-client gives the entrypoint `pg_isready`, so the CRM waits for a
# usable database rather than crash-looping until compose's restart policy
# happens to line up.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ ./shared/
COPY core_django/ ./core_django/
COPY local_agent/ ./local_agent/
COPY conftest.py pytest.ini ./
COPY docker/ ./docker/
RUN chmod +x ./docker/*.sh

# Baked into the image so a container never needs a writable static dir.
# DJANGO_DEBUG is forced off here only so collectstatic picks the hashed
# manifest storage; it does not affect runtime, which reads the real env.
#
# The two throwaway values are for this RUN only. settings.py refuses to import
# with DEBUG off and either a dev SECRET_KEY or no DATABASE_URL, and collectstatic
# imports settings even though it touches neither. They are deliberately NOT ENV:
# a real secret baked into a layer lives forever in the image history, and a real
# DATABASE_URL defaulted into the image is precisely the silent-wrong-database
# failure those guards exist to prevent.
RUN DJANGO_DEBUG=False \
    DJANGO_SECRET_KEY=build-only-never-used-at-runtime \
    DATABASE_URL=postgres://build:build@127.0.0.1:5432/build \
    python core_django/manage.py collectstatic --noinput

# Nothing here needs root, and a hosted container is worth the two lines. Done
# after collectstatic so the build can still write staticfiles/.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000 8111

# The full argv lives in the image, not only in docker-compose.yml -- `docker run
# ignite-crm` used to exec the entrypoint with no arguments, so its final
# `exec "$@"` ran nothing and the container exited silently. A hosting platform
# starts the image exactly that way.
#
# Routed through `sh -c` on purpose: PORT is injected by the platform at RUN
# time, and a JSON-array CMD performs no variable expansion. A server listening
# on the wrong port presents as "deploy succeeded, site unreachable", with
# nothing in the logs to say why.
CMD ["./docker/entrypoint-crm.sh", "sh", "-c", \
     "exec gunicorn config.wsgi:application \
        --chdir /app/core_django \
        --bind 0.0.0.0:${PORT:-8000} \
        --workers ${WEB_CONCURRENCY:-2} \
        --threads ${WEB_THREADS:-4} \
        --timeout ${WEB_TIMEOUT:-120} \
        --access-logfile -"]
