FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Europe/Warsaw
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY hunter ./hunter
COPY sql ./sql
COPY config.example.yaml ./

ENTRYPOINT ["python", "-m", "hunter"]
CMD ["run"]
