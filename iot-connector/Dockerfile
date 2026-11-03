ARG BUILD_FROM
FROM ${BUILD_FROM}

# Bash/io für Healthcheck und Basis-Debugging
RUN apk add --no-cache bash io

COPY rootfs/ /
RUN chmod a+x /run.sh /ingress.sh

WORKDIR /app
COPY app/requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r /app/requirements.txt

COPY app/ /app/

LABEL io.hass.type="addon" \
      io.hass.arch="aarch64|amd64" \
      org.opencontainers.image.source="https://github.com/Gimtiese/iot-connector-ha-addon"