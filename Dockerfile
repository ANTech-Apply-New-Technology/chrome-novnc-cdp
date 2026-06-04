FROM debian:13-slim

# Locale (ANTech: en_US default + Europe/Stockholm tz for Swedish/EU usage)
ENV LANG=en_US.UTF-8
ENV LANGUAGE=en_US.UTF-8
ENV LC_ALL=en_US.UTF-8
ENV TZ="Europe/Stockholm"

# VNC Server Title(w/o spaces)
ENV VNC_TITLE="Chromium"
# VNC Resolution(720p is preferable)
ENV VNC_RESOLUTION="1280x720"
# VNC Shared Mode
ENV VNC_SHARED=false
# Local Display Server Port
ENV DISPLAY=:0
# Port settings
ENV PORT=8080
ENV NOVNC_PORT=$PORT

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        supervisor \
        bash \
        python3 \
        python3-requests \
        sed \
        xvfb \
        x11vnc \
        novnc \
        openbox \
        socat \
        libnss3 \
        libasound2 \
        fonts-noto-cjk \
        fonts-noto-core \
        fonts-dejavu-core \
        ca-certificates \
        tzdata \
        locales \
        python3-pip && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install uv and aiohttp.
# ANTech: pin websockify>=0.11 so the BasicHTTPAuth plugin path used by the
# noVNC HTTP Basic auth layer is guaranteed available (the distro novnc pulls
# in a websockify, but we install an explicit modern one to be safe).
RUN pip install uv aiohttp 'websockify>=0.11' --break-system-packages

# Install chromium using playwright via uvx
RUN uvx playwright install chromium --with-deps --no-shell

# Create a symlink to the installed chromium
RUN CHROME_PATH=$(find /root/.cache/ms-playwright/ -type f -name chrome | head -n 1) && \
  ln -s $CHROME_PATH /usr/bin/chromium

# Configure locale (ANTech: enable en_US.UTF-8 and sv_SE.UTF-8)
RUN sed -i 's/^# *\(en_US.UTF-8\)/\1/' /etc/locale.gen && \
  sed -i 's/^# *\(sv_SE.UTF-8\)/\1/' /etc/locale.gen && \
  locale-gen

# Configure timezone
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && \
  echo $TZ > /etc/timezone

COPY assets/ /

ENTRYPOINT ["supervisord", "-l", "/var/log/supervisord.log", "-c"]

CMD ["/config/supervisord.conf"]
