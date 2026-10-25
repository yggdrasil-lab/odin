FROM python:3.12-alpine

WORKDIR /app

COPY shim.py /app/shim.py

EXPOSE 8000

ENV PYTHONUNBUFFERED=1

CMD ["python3", "/app/shim.py", "--host", "0.0.0.0", "--port", "8000"]
