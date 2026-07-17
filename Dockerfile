# Dockerfile — OPTIONAL / LEGACY.
#
# The SUPPORTED way to run this bot is bare-metal via ./install.sh + systemd
# (see README.md and docs/UBUNTU_VPS_SETUP.md). Docker is kept as an alternative
# for those who prefer it. This image reproduces what install.sh does: it installs
# Deno, the Python deps (incl. the yt-dlp PO-token plugin), AND clones + builds the
# bgutil PO-token provider so YouTube works out of the box.

FROM python:3.11-slim

# --- 1. System packages: ffmpeg + curl/unzip + canvas native build libs ---
# The canvas libs (libcairo2-dev etc.) + build-essential are required to compile
# the bgutil provider's native canvas FFI via `deno install --allow-scripts`.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        unzip \
        git \
        ca-certificates \
        build-essential \
        pkg-config \
        libcairo2-dev \
        libpango1.0-dev \
        libjpeg-dev \
        libgif-dev \
        librsvg2-dev \
    && rm -rf /var/lib/apt/lists/*

# --- 2. Deno runtime (>= 2.0) for the PO-token provider ---
RUN curl -fsSL https://deno.land/install.sh | sh -s -- -y
ENV DENO_INSTALL="/root/.deno"
ENV PATH="$DENO_INSTALL/bin:$PATH"

WORKDIR /app

# --- 3. Python dependencies (incl. bgutil-ytdlp-pot-provider yt-dlp plugin) ---
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- 4. Clone + build the bgutil PO-token provider (pinned ref) ---
# Mirrors install.sh step 5. Builds the native canvas FFI so the provider can
# solve YouTube's browser-fingerprint challenge. Default YTDLP_POT_PROVIDER_REF
# in config.py is 1.3.1; override at build time with --build-arg POT_REF=...
#
# IMPORTANT: we clone into /opt/bgutil-provider (NOT /app/...) because
# docker-compose.yml bind-mounts the host project dir over /app. Anything we put
# in /app during the build is shadowed by the bind-mount at runtime; /opt is not,
# so the provider survives. config.py honors YTDLP_POT_PROVIDER_PATH from env.
ARG POT_REF=1.3.1
ENV YTDLP_POT_PROVIDER_PATH=/opt/bgutil-provider/server
RUN git clone --single-branch --branch "${POT_REF}" \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /tmp/bgutil \
    && mkdir -p /opt/bgutil-provider \
    && mv /tmp/bgutil/server /opt/bgutil-provider/server \
    && (mv /tmp/bgutil/deno.json /opt/bgutil-provider/deno.json 2>/dev/null || true) \
    && rm -rf /tmp/bgutil \
    && cd /opt/bgutil-provider/server \
    && (deno install --allow-scripts --frozen || deno install --allow-scripts)

# --- 5. App code + runtime cache dir ---
COPY . .
RUN mkdir -p /app/cache

CMD ["python", "main.py"]
