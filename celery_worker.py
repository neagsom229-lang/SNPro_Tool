"""
Entrypoint for the Celery CLI (`celery -A celery_worker.celery worker ...`).

Celery's CLI just does a plain import — it won't call your Flask app
factory on its own. Importing this module runs create_app(), which
(per app/__init__.py) calls make_celery(app) and populates the
`celery` global in extensions.py before we re-export it here.
"""
from app import create_app

# Creating the app has the side effect of configuring `celery`
# (broker/backend URLs, task_routes, SSL options, etc.) via make_celery().
flask_app = create_app()