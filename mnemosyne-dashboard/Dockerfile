FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Clone the dashboard source
RUN git clone https://github.com/wysie/mnemosyne-dashboard.git .

# Install requirements
RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8080

CMD ["python", "app.py"]
