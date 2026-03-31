FROM lscr.io/linuxserver/webtop:debian-kde

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        chromium \
        imagemagick \
        xdotool \
    && rm -rf /var/lib/apt/lists/*
