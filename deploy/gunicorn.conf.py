"""gunicorn config for CapsuleHotOrNot.

Threads, not extra processes: the app is I/O-bound (MySQL, Steam scraping) and
the background playtime sync + the in-process cluster-cache memo both live in
worker memory, so fewer workers means fewer duplicate caches and syncs.
"""
import multiprocessing
import os

bind = os.environ.get('BIND', '127.0.0.1:8000')   # Caddy fronts this
workers = int(os.environ.get('WEB_WORKERS', 2))
threads = int(os.environ.get('WEB_THREADS', 4))
worker_class = 'gthread'

# A recluster (/dev/vectors?refresh=1) legitimately runs for minutes.
timeout = int(os.environ.get('WEB_TIMEOUT', 900))
graceful_timeout = 60
keepalive = 5

# Uploads are 25 MB max; leave headroom for the multipart body.
limit_request_line = 8190

accesslog = '-'
errorlog = '-'
loglevel = os.environ.get('LOG_LEVEL', 'info')
proc_name = 'capsulehotornot'

# t-SNE/BLAS will happily grab every core per worker and thrash. Keep each
# worker single-threaded in BLAS terms; gthread handles the concurrency.
os.environ.setdefault('OMP_NUM_THREADS', '1')
os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
os.environ.setdefault('MKL_NUM_THREADS', '1')
os.environ.setdefault('LOKY_MAX_CPU_COUNT', str(max(1, multiprocessing.cpu_count() // 2)))
