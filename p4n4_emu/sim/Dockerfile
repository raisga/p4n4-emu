FROM python:3.11-slim

WORKDIR /app

COPY p4n4_emu/sim/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY p4n4_emu/ ./p4n4_emu/

ENV MQTT_HOST=p4n4-mqtt
ENV MQTT_PORT=1883
ENV SIM_INTERVAL_SEC=2.0
ENV SIM_DEVICE_COUNT=1

CMD ["python", "-m", "p4n4_emu.sim.sensor_sim"]
