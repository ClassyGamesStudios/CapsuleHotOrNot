"""WSGI entry point for a real web server (gunicorn on Linux, waitress on Windows).

    gunicorn  -c deploy/gunicorn.conf.py wsgi:application
    waitress-serve --listen=127.0.0.1:8000 wsgi:application

Unlike `python app.py` (the dev server), this applies migrations and seeds once
at boot and refuses to start with insecure defaults when CHON_ENV=production.
"""
import logging

import pymysql

import app as _app
import db

# The landing page needs no database, so a missing one shouldn't take the whole
# site down: log it loudly and let the DB-backed pages fail on their own.
try:
    db.init_db()
    db.seed_tags()
    db.seed_image_categories()
except pymysql.err.OperationalError as exc:
    logging.getLogger('capsulehotornot').error(
        'Database unreachable at boot (%s). Only pages that need no database '
        '(the landing page) will work.', exc)

if _app.IS_PRODUCTION:
    _app.check_production_config()

application = _app.app        # gunicorn/waitress look for this
app = application             # convenience alias for `gunicorn wsgi:app`
