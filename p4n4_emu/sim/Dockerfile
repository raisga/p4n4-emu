FROM python:3.11-slim

WORKDIR /app

COPY p4n4_emu/sim/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY p4n4_emu/ ./p4n4_emu/

# The port, interval and device count default in sensor_sim.py: set here, they would
# override a scenario's interval and the TLS port
ENV MQTT_HOST=p4n4-mqtt

CMD ["python", "-m", "p4n4_emu.sim.sensor_sim"]
