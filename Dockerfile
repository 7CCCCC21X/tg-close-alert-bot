FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STATE_DB=/data/bot.sqlite3

WORKDIR /app
COPY main.py ./

RUN python -m py_compile main.py

CMD ["python", "-u", "main.py"]
