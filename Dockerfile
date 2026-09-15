FROM docker.io/library/python:3.12-slim

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/

ENV BOT_TOKEN= ALLOWED_USERIDS= ADMIN_USERIDS= STATE_FILE=/app/ytsub-state.json CHECK_INTERVAL_SEC=300 SUBSCRIPTION_SYNC_INTERVAL_SEC=43200 MAX_POSTS_PER_MIN=10 GOOGLE_CLIENT_ID= GOOGLE_CLIENT_SECRET= GOOGLE_CLIENT_SECRET_FILE=

ENTRYPOINT ["python3", "main.py"]
