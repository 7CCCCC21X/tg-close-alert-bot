FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STATE_DB=/data/bot.sqlite3
WORKDIR /app
COPY main.py ./
COPY tests ./tests
# Tests are fully offline and require no token or market data.
RUN python -m unittest discover -s tests -q
CMD ["python", "-u", "main.py"]
