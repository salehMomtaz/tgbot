FROM python:3.11-slim

# Install ffmpeg, curl, and unzip (required for deno installer)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Download and install Deno (the recommended JS runtime for yt-dlp)
RUN curl -fsSL https://deno.land/install.sh | sh

# Configure Deno in the PATH so yt-dlp can locate it automatically
ENV DENO_INSTALL="/root/.deno"
ENV PATH="$DENO_INSTALL/bin:$PATH"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create space for config and media cache
RUN mkdir -p /app/cache

COPY . .

CMD ["python", "main.py"]
