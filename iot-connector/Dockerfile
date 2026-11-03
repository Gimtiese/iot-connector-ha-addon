ARG BUILD_FROM
FROM ${BUILD_FROM}

# Bash für die Add-on-Shell (im Basis-Image enthalten, explizit für Debugging)
RUN apk add --no-cache bash

COPY rootfs/ /
RUN chmod a+x /run.sh

WORKDIR /app
# App als Paket nach /app/app – run.sh führt `python3 -m app.main` aus /app aus
COPY app/requirements.txt /app/app/requirements.txt
RUN pip3 install --no-cache-dir -r /app/app/requirements.txt

COPY app/ /app/app/

# HAOS startet das Basis-Image nur mit ENTRYPOINT /init (s6-overlay v3) –
# ohne CMD würde die App nie gestartet (init: false, kein s6-Dienst).
CMD [ "/run.sh" ]

LABEL io.hass.type="addon" \
      io.hass.arch="aarch64|amd64" \
      org.opencontainers.image.source="https://github.com/Gimtiese/iot-connector-ha-addon"