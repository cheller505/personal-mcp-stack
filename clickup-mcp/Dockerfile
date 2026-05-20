FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV HOME=/data
ENV PYTHONUNBUFFERED=1
RUN mkdir -p /data && chmod 700 /data
VOLUME ["/data"]

CMD ["python", "-u", "main.py"]
