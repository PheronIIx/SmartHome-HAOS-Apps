FROM python:3.13-alpine
WORKDIR /srv/smarthome-deploy
COPY app/ ./app/
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8099
CMD ["python", "-m", "app.server"]
