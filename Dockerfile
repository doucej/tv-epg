FROM python:3.13-slim

WORKDIR /app
COPY hdhr_epg.py docker-entrypoint.sh ./
RUN chmod +x ./docker-entrypoint.sh

VOLUME /epg
ENTRYPOINT ["./docker-entrypoint.sh"]
