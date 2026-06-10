FROM python:3.11-slim

# Install ffmpeg, curl, and nodejs (required for solving YouTube signature challenges)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create space for config and media cache
RUN mkdir -p /app/cache

COPY . .

CMD ["python", "main.py"]
