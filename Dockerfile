FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY giftbot giftbot
ENV DB_PATH=/data/giftbot.db SESSION=/data/watcher PYTHONUNBUFFERED=1
VOLUME /data
CMD ["python", "-m", "giftbot"]
