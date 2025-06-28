FROM python:3.13

ENV PYTHONUNBUFFERED 1

WORKDIR /app/

RUN apt-get update && \
    apt-get install -y python3-dev cmake curl gcc build-essential && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

ENV PYTHONPATH=/app

COPY requirements.txt /requirements.txt
RUN pip install --upgrade pip --progress-bar off && pip install -r /requirements.txt --progress-bar off

COPY . /app
COPY ./app/scripts/start-gunicorn.sh /start-gunicorn.sh
RUN chmod u+x /start-gunicorn.sh

CMD ["/start-gunicorn.sh"]