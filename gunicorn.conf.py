# Gunicorn configuration for Forcelet (production).
#
# SQLite serializes writers, so a single worker with threads is the safe
# default. Scale reads with more threads, not more workers. If you outgrow
# SQLite, move the Store to Postgres first, then raise `workers`.
workers = 1
worker_class = "gthread"
threads = 8
bind = "127.0.0.1:8000"
timeout = 60
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = "info"
# Run behind a TLS-terminating reverse proxy (Caddy/nginx); see docs/DEPLOYMENT.md.
forwarded_allow_ips = "127.0.0.1"
