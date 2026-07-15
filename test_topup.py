"""Smoke test for topup.py using gpiozero's mock pin factory.

Runs the TopUp and SkimmerSweep state machines with millisecond-scale
timings on simulated pins and asserts that every transition honours the
timing and safety rules. Needs no GPIO hardware:

    python3 test_topup.py
"""
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from gpiozero import Device, DigitalInputDevice, DigitalOutputDevice
from gpiozero.pins.mock import MockFactory

Device.pin_factory = MockFactory()

import topup as m

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        failures.append(name)


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


sensor = DigitalInputDevice(21, pull_up=None, active_state=False)
pump = DigitalOutputDevice(8, initial_value=False)
motor = DigitalOutputDevice(9, initial_value=False)
sensor_pin = Device.pin_factory.pin(21)

MIN_OPEN = 0.15
MIN_CLOSE = 0.4
SWEEP_OFF = 0.3
SWEEP_ON = 0.15

# --- Scenario 1: start with level FULL -> pump stays off (lockout) ---
sensor_pin.drive_low()  # LOW = wet = full
check("sensor reads full", sensor.is_active)

stop = threading.Event()
tu = m.TopUp(sensor, pump, stop, min_open=MIN_OPEN, min_close=MIN_CLOSE)
sw = m.SkimmerSweep(motor, stop, on_time=SWEEP_ON, off_time=SWEEP_OFF)
t1 = threading.Thread(target=tu.run, daemon=True)
t2 = threading.Thread(target=sw.run, daemon=True)
t0 = time.monotonic()
t1.start()
t2.start()

time.sleep(0.05)
check("pump off at start (level full)", not pump.is_active)
check("motor off at start", not motor.is_active)

# Water drops during lockout -> pump must NOT start before lockout expires
time.sleep(0.1)
sensor_pin.drive_high()  # HIGH = dry = water low
time.sleep(0.1)  # now ~0.25s elapsed, lockout is 0.4s
check("pump held off during lockout despite low water", not pump.is_active)

# After lockout expires, pump turns on
check("pump ON after lockout with low water",
      wait_for(lambda: pump.is_active, timeout=0.5))
pump_on_at = time.monotonic()
check("lockout duration respected (>= min_close)",
      pump_on_at - t0 >= MIN_CLOSE - 0.02)

# Level restored immediately -> pump must keep running for min_open
time.sleep(0.03)
sensor_pin.drive_low()  # full again
time.sleep(0.05)
check("pump still ON before min run elapsed", pump.is_active)
check("pump OFF once min run elapsed and level full",
      wait_for(lambda: not pump.is_active, timeout=0.5))
check("min run respected (>= min_open)",
      time.monotonic() - pump_on_at >= MIN_OPEN - 0.02)

# --- Sweep: off period first, then on for SWEEP_ON ---
check("sweep motor ON after off period",
      wait_for(lambda: motor.is_active, timeout=SWEEP_OFF + 0.3))
motor_on_at = time.monotonic()
check("sweep off period respected", motor_on_at - t0 >= SWEEP_OFF - 0.02)
check("sweep motor OFF after on period",
      wait_for(lambda: not motor.is_active, timeout=SWEEP_ON + 0.3))

# --- Scenario 2: pump ON at shutdown -> everything switched off ---
stop2 = threading.Event()
pump2 = DigitalOutputDevice(7, initial_value=False)
sensor_pin.drive_high()  # water low
tu2 = m.TopUp(sensor, pump2, stop2, min_open=5, min_close=5)
t3 = threading.Thread(target=tu2.run, daemon=True)
t3.start()
check("pump2 ON immediately at start with low water",
      wait_for(lambda: pump2.is_active, timeout=0.5))
stop2.set()
tu2.wake()
t3.join(timeout=2)
check("topup thread exits promptly on stop", not t3.is_alive())
check("pump2 OFF after stop", not pump2.is_active)

# Shut down scenario-1 threads
stop.set()
tu.wake()
t1.join(timeout=2)
t2.join(timeout=2)
check("threads exited", not t1.is_alive() and not t2.is_alive())
check("pump off after stop", not pump.is_active)
check("motor off after stop", not motor.is_active)
check("no crash flags", not tu.crashed and not sw.crashed and not tu2.crashed)

# --- Config loading ---
cfg = m.load_config(Path(__file__).with_name("config.json"))
check("shipped config.json loads and validates",
      cfg["topup_min_close_s"] == 14400 and cfg["water_sensor_gpio"] == 21)

tmp = Path(tempfile.mkdtemp())
bad_missing = tmp / "missing_key.json"
bad_missing.write_text(json.dumps({k: 1 for k in m.REQUIRED_CONFIG_KEYS
                                   if k != "sweep_on_s"}))
try:
    m.load_config(bad_missing)
    check("missing key rejected", False)
except ValueError as exc:
    check("missing key rejected", "sweep_on_s" in str(exc))

bad_value = tmp / "bad_value.json"
bad_value.write_text(json.dumps(
    dict({k: 1 for k in m.REQUIRED_CONFIG_KEYS}, topup_min_open_s=-5)))
try:
    m.load_config(bad_value)
    check("negative value rejected", False)
except ValueError as exc:
    check("negative value rejected", "topup_min_open_s" in str(exc))

try:
    m.load_config(tmp / "nonexistent.json")
    check("missing file raises OSError", False)
except OSError:
    check("missing file raises OSError", True)

print()
if failures:
    print("FAILED:", failures)
    sys.exit(1)
print("ALL CHECKS PASSED")
