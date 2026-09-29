"""Run the Forcelet server (API + web UI)."""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import send_from_directory
from forcelet.api import create_app

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("FORCELET_DB") or os.path.join(BASE, "forcelet.db")

app = create_app(DB)


def _scheduler_loop():
    """Background thread: run due scheduled jobs every 60 seconds."""
    import threading
    import time
    import traceback

    def loop():
        while True:
            time.sleep(60)
            try:
                from forcelet import automation
                automation.run_due_scheduled_jobs(
                    app.mf_store, app.mf_registry, app.mf_security)
            except Exception:
                traceback.print_exc()

    t = threading.Thread(target=loop, daemon=True, name="forcelet-scheduler")
    t.start()


_scheduler_loop()


@app.get("/")
def index():
    return send_from_directory(os.path.join(BASE, "web"), "index.html")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    print("⚡ Forcelet running at http://localhost:5000")
    print("   Built by Suresh Itha")
    print("   Demo users: admin (System Administrator), maya (manager), leo (rep), ana (read-only)")
    print("   Demo password for all users: forcelet (change it after first login)")
    app.run(host=args.host, port=args.port)
