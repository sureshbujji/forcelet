FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV FORCELET_DB=/data/forcelet.db
VOLUME /data
EXPOSE 5000

CMD ["python", "run.py", "--host", "0.0.0.0", "--port", "5000"]
