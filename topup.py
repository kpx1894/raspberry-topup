#!/usr/bin/env python3
"""Aquarium auto top-up and skimmer sweep controller for Raspberry Pi Zero W.

Uses gpiozero with BCM pin numbering.

Usage:
    python3 topup.py [config.json]

GPIO pin numbers and timings come from a JSON config file (by default
``config.json`` next to this script). All keys are required:
    water_sensor_gpio, topup_pump_gpio, sweep_motor_gpio,
    topup_min_open_s, topup_min_close_s, sweep_on_s, sweep_off_s
Times are in seconds. The program refuses to start on a missing or
invalid config.

Behaviour
---------
Top-up pump:
  * When the level sensor reports LOW water, the pump turns on.
  * The pump runs for at least TOPUP_MIN_OPEN_S, then keeps running until
    the sensor reports the level restored, at which point it turns off.
  * After turning off, the pump stays off for at least TOPUP_MIN_CLOSE_S
    (lockout), then turns on again as soon as the sensor reports LOW water.
  * At start-up: pump turns on immediately if the level is low; otherwise
    the lockout period starts.

Skimmer sweep motor:
  * Off for SWEEP_OFF_S, on for SWEEP_ON_S, repeating; starts with the
    off period.

Safety
------
  * Both outputs are initialised OFF and forced OFF on exit (SIGINT,
    SIGTERM, or an unexpected error).
  * If a control thread fails, it switches its own output off and stops
    the whole program with a non-zero exit code so a supervisor
    (e.g. systemd with Restart=on-failure) can restart it.
  * There is NO maximum pump run time: once on, the pump runs until the
    sensor reports the level restored.

Hardware assumptions
--------------------
  * The optical level sensor drives its output HIGH when dry and LOW
    when wet; wet means the level is FULL. The sensor drives the line,
    so no internal pull resistor is used.
  * Pump and sweep motor relays are active-high.
  * If the configured output pins are SPI pins (e.g. GPIO 8/9), the SPI
    interface must be disabled (raspi-config -> Interface Options).
"""

import json
import logging
import signal
import sys
import threading
from pathlib import Path

from gpiozero import DigitalInputDevice, DigitalOutputDevice

DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.json")
REQUIRED_CONFIG_KEYS = (
    "water_sensor_gpio", "topup_pump_gpio", "sweep_motor_gpio",
    "topup_min_open_s", "topup_min_close_s", "sweep_on_s", "sweep_off_s")

log = logging.getLogger("topup")


def load_config(path):
    """Read settings from a JSON file; every required key must be a
    positive number."""
    with open(path) as f:
        config = json.load(f)
    for key in REQUIRED_CONFIG_KEYS:
        value = config.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) \
                or value <= 0:
            raise ValueError(
                f"config {path}: missing or invalid '{key}' "
                f"(expected a positive number, got {value!r})")
    return config


class TopUp:
    """Keeps the water level topped up.

    Two states:
      OPEN  (pump on):  after ``min_open`` s, off as soon as level is full.
      CLOSE (pump off): after ``min_close`` s, on as soon as level is low.

    ``sensor`` must expose ``is_active`` (True = level full) and the
    ``when_activated``/``when_deactivated`` callbacks; ``pump`` must
    expose ``on()``/``off()``. The thread blocks on events between
    transitions and consumes no CPU while idle.
    """

    def __init__(self, sensor, pump, stop, min_open, min_close):
        self._sensor = sensor
        self._pump = pump
        self._stop = stop
        self._min_open = min_open
        self._min_close = min_close
        self._level_changed = threading.Condition()
        self.crashed = False
        sensor.when_activated = self.wake
        sensor.when_deactivated = self.wake

    def wake(self):
        """Wake a pending level wait (sensor edge, or stop requested)."""
        with self._level_changed:
            self._level_changed.notify_all()

    def _wait_for_level(self, full):
        """Block until the sensor reports full (True) / low (False) water,
        or until stop is requested."""
        with self._level_changed:
            self._level_changed.wait_for(
                lambda: self._stop.is_set() or self._sensor.is_active == full)

    def _open(self):
        self._pump.on()
        log.info("top-up: water low, pump ON (min run %ds)", self._min_open)

    def _close(self):
        self._pump.off()
        log.info("top-up: level restored, pump OFF (lockout %ds)",
                 self._min_close)

    def run(self):
        try:
            if self._sensor.is_active:
                pump_open = False
                log.info("top-up: level full at start, pump off (lockout %ds)",
                         self._min_close)
            else:
                self._open()
                pump_open = True
            while not self._stop.is_set():
                if pump_open:
                    self._stop.wait(self._min_open)
                    self._wait_for_level(full=True)
                    if self._stop.is_set():
                        break
                    self._close()
                    pump_open = False
                else:
                    self._stop.wait(self._min_close)
                    self._wait_for_level(full=False)
                    if self._stop.is_set():
                        break
                    self._open()
                    pump_open = True
        except Exception:
            self.crashed = True
            log.exception("top-up: control thread failed")
        finally:
            self._pump.off()
            self._stop.set()  # fail loud: bring the whole program down
            log.info("top-up: stopped, pump off")


class SkimmerSweep:
    """Runs the sweep motor for ``on_time`` s every ``off_time`` s,
    starting with the off period."""

    def __init__(self, motor, stop, on_time, off_time):
        self._motor = motor
        self._stop = stop
        self._on_time = on_time
        self._off_time = off_time
        self.crashed = False

    def run(self):
        try:
            while not self._stop.wait(self._off_time):
                self._motor.on()
                log.info("sweep: motor ON for %ds", self._on_time)
                self._stop.wait(self._on_time)
                self._motor.off()
                log.info("sweep: motor OFF for %ds", self._off_time)
        except Exception:
            self.crashed = True
            log.exception("sweep: control thread failed")
            self._stop.set()  # fail loud: bring the whole program down
        finally:
            self._motor.off()
            log.info("sweep: stopped, motor off")


def main(argv):
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    config_path = argv[1] if len(argv) > 1 else DEFAULT_CONFIG_PATH
    try:
        config = load_config(config_path)
    except (OSError, ValueError) as exc:
        log.error("cannot load config: %s", exc)
        return 1

    # Sensor drives the line itself (no pull resistor); LOW = wet = full,
    # so is_active means "level full".
    sensor = DigitalInputDevice(config["water_sensor_gpio"],
                                pull_up=None, active_state=False)
    pump = DigitalOutputDevice(config["topup_pump_gpio"], initial_value=False)
    motor = DigitalOutputDevice(config["sweep_motor_gpio"],
                                initial_value=False)

    stop = threading.Event()
    topup = TopUp(sensor, pump, stop,
                  min_open=config["topup_min_open_s"],
                  min_close=config["topup_min_close_s"])
    sweep = SkimmerSweep(motor, stop,
                         on_time=config["sweep_on_s"],
                         off_time=config["sweep_off_s"])

    def handle_signal(signum, frame):
        log.info("received %s, shutting down", signal.Signals(signum).name)
        stop.set()
        topup.wake()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    threads = [threading.Thread(target=topup.run, name="topup", daemon=True),
               threading.Thread(target=sweep.run, name="sweep", daemon=True)]
    log.info("starting: sensor GPIO%d, pump GPIO%d, sweep motor GPIO%d",
             config["water_sensor_gpio"], config["topup_pump_gpio"],
             config["sweep_motor_gpio"])
    try:
        for thread in threads:
            thread.start()
        stop.wait()
        topup.wake()  # release a thread stuck in a level wait
        for thread in threads:
            thread.join(timeout=5)
    finally:
        pump.off()
        motor.off()
        log.info("outputs off, exiting")
    return 1 if (topup.crashed or sweep.crashed) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
