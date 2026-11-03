#!/usr/bin/with-contenv bashio
# Add-on-Start (HAOS): App übernimmt Supervisor-Token und Optionen aus der Umgebung
cd /app
exec python3 -m app.main