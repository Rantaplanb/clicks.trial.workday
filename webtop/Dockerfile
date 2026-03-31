FROM lscr.io/linuxserver/webtop:debian-kde

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        chromium \
        imagemagick \
        xdotool \
        x11vnc \
        novnc \
    && rm -rf /var/lib/apt/lists/*
