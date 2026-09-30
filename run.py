"""Run the Forcelet server (API + web UI).

Development server. For production, serve with gunicorn (see wsgi.py,
gunicorn.conf.py, and docs/DEPLOYMENT.md) behind a TLS-terminating proxy.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from forcelet.api import create_app
from forcelet.scheduler import start_background_thread

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("FORCELET_DB") or os.path.join(BASE, "forcelet.db")

app = create_app(DB)
start_background_thread(app.mf_store, app.mf_registry, app.mf_security)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    print("Forcelet running at http://localhost:5000  (development server)")
    print("   Built by Suresh Itha")
    print("   Demo users: admin (System Administrator), maya (manager), leo (rep), ana (read-only)")
    print("   For production use, see docs/DEPLOYMENT.md")
    app.run(host=args.host, port=args.port)
