"""Register-level models of common sensor parts, fed by the simulator's waves.

Each model answers on its bus the way the real part does (chip id, calibration
data, control registers, data registers in the part's own encoding), so a real
driver reads plausible values through the smbus2 / spidev / sysfs shims.
"""

from p4n4_emu.hw.devices.ads1115 import ADS1115
from p4n4_emu.hw.devices.bme280 import BME280
from p4n4_emu.hw.devices.ds18b20 import DS18B20
from p4n4_emu.hw.devices.mcp3008 import MCP3008
from p4n4_emu.hw.devices.mpu6050 import MPU6050

__all__ = ["ADS1115", "BME280", "DS18B20", "MCP3008", "MPU6050"]
