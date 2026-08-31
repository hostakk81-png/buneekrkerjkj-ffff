FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY . /app

RUN mkdir -p /app/data /app/public/uploads

EXPOSE 8080
CMD ["python", "run.py"]

