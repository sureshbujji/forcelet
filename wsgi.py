"""Production WSGI entrypoint: ``gunicorn wsgi:app``.

Configuration via environment variables (see docs/DEPLOYMENT.md):
  FORCELET_DB            path to the SQLite database file
  FORCELET_SCHEDULER     "0" disables the in-process scheduler thread
  FORCELET_BEHIND_PROXY  "1" when running behind a TLS-terminating proxy
  FORCELET_BACKUP_DIR    where online backups are written
  FORCELET_ENC_KEY       encryption key for sensitive fields
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from forcelet.api import create_app  # noqa: E402

DB = os.environ.get("FORCELET_DB") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "forcelet.db")

app = create_app(DB)

# In production the scheduler usually runs as a separate process
# (FORCELET_SCHEDULER=0 here + `python -m forcelet.scheduler_run`), but with
# the DB heartbeat lock it is also safe to leave enabled in a single worker.
if os.environ.get("FORCELET_SCHEDULER", "1") != "0":
    from forcelet.scheduler import start_background_thread  # noqa: E402
    start_background_thread(app.mf_store, app.mf_registry, app.mf_security)
