from datetime import timedelta
from arq.connections import RedisSettings
from app.worker import process_job


class WorkerSettings:
    functions = [process_job]

    redis_settings = RedisSettings(
        host="localhost",
        port=6379
    )

    # Allow jobs to run for 15 minutes
    job_timeout = timedelta(minutes=15)