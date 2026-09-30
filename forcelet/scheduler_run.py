"""Standalone scheduler process: ``python -m forcelet.scheduler_run``.

Runs due scheduled jobs every 60 seconds. Use when the web workers run with
FORCELET_SCHEDULER=0. Safe to run alongside web workers: the DB heartbeat
lock ensures only one runner executes jobs.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forcelet.api import create_app  # noqa: E402
from forcelet import scheduler  # noqa: E402

DB = os.environ.get("FORCELET_DB") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "forcelet.db")

app = create_app(DB)

if __name__ == "__main__":
    print(f"Forcelet scheduler running against {DB} (Ctrl-C to stop)")
    while True:
        scheduler.run_once(app.mf_store, app.mf_registry, app.mf_security)
        time.sleep(60)
