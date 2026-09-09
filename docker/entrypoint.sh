#!/bin/sh
# Selects which of the image's roles to run. One image, one process per container.
#
#   api       the FastAPI service
#   ui        the Gradio demo UI
#   migrate   apply migrations and exit
#
# Migrations are their own role rather than something the API does on the way up. Two API
# containers starting at once would otherwise run them concurrently, and "it worked when
# there was one replica" is not a property worth relying on.
set -eu

ROLE="${1:-api}"

case "$ROLE" in
  api)
    exec uvicorn retail_support_agent.api:create_app --factory \
      --host "${RSA_API_HOST:-0.0.0.0}" --port "${RSA_API_PORT:-8000}"
    ;;
  ui)
    exec python -m retail_support_agent.ui
    ;;
  migrate)
    exec alembic upgrade head
    ;;
  *)
    # Anything else is run as given, so that `docker run <image> python -c ...` still works
    # for diagnosis without a second image.
    exec "$@"
    ;;
esac
