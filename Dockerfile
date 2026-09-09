# syntax=docker/dockerfile:1
#
# One image, two roles: the API and the demo UI are separate processes on separate ports,
# started from the same image by the entrypoint's first argument. Putting both behind one
# supervisor inside one container would hide which of them failed.
#
# The base image is pinned by digest, not by tag. A tag moves, and a demo whose image
# changes underneath it cannot be compared with the run before it.
FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS builder

# uv is pinned to the version this repository's lockfile was written with.
COPY --from=ghcr.io/astral-sh/uv:0.9.29 /uv /usr/local/bin/uv

# tau2 is pinned to a git commit rather than a released version, so resolving it needs
# git. It lives in the builder stage only; the runtime stage has no use for it and a
# smaller image is a smaller thing to pull.
RUN apt-get update  && apt-get install -y --no-install-recommends git  && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, project second, so that editing source code does not reinstall
# 144 packages. --frozen refuses to update the lockfile: an image whose dependency set is
# resolved at build time is not the one that was tested.
#
# The demo group carries tau2, which the running service imports through RetailDomain.
# The dev and bench groups stay out: nothing here runs tests or reports traces.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --group demo --no-install-project

COPY src/ ./src/
COPY migrations/ ./migrations/
COPY alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --group demo


# A stage that can run this repository's own test suite against the image's resolved
# dependency set. It is not part of the runtime image and adds nothing to it.
#
# The suite is not baked in. It reads repository artifacts the runtime image does not carry
# - frozen baselines, task sets, JSON schemas, the upstream manifest, the environment
# template - so copying a subset in would mean choosing which tests to run. Instead the
# repository is mounted and the whole suite runs, using this stage's interpreter and
# packages:
#
#   docker build --target test -t rsa-test:local .
#   docker run --rm -v "$PWD":/repo -w /repo -e TAU2_DATA_DIR=/repo/third_party/tau2-bench/data #     rsa-test:local /app/.venv/bin/python -m pytest -q
#
# The tests then run on the versions the demo image runs.
FROM builder AS test
RUN --mount=type=cache,target=/root/.cache/uv     uv sync --frozen --group demo
ENTRYPOINT []
CMD ["/app/.venv/bin/python", "-m", "pytest", "-q"]

FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS runtime

# A demo that runs as root is a demo nobody should put on the public internet.
RUN useradd --create-home --uid 10001 app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src /app/src
COPY --from=builder --chown=app:app /app/migrations /app/migrations
COPY --from=builder --chown=app:app /app/alembic.ini /app/alembic.ini

# Only the retail domain's data, 34.6 MB of the checkout's 888 MB. .dockerignore is what
# keeps the rest out of the build context; this line is what puts the needed part in.
# The checkout it comes from is SHA256-verified by scripts/fetch_tau2.py, which has to
# have run before this image is built.
COPY --chown=app:app third_party/tau2-bench/data/tau2/domains/retail \
     /app/data/tau2/domains/retail
COPY --chown=app:app third_party/tau2-bench/data/tau2/user_simulator \
     /app/data/tau2/user_simulator

COPY --chown=app:app docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TAU2_DATA_DIR=/app/data

USER app
EXPOSE 8000 7860
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["api"]
