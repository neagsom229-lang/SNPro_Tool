web: gunicorn run:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120
worker: celery -A celery_worker.celery worker --loglevel=info --concurrency=2 -Q heavy,light -B
