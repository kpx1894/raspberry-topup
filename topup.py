#!/usr/bin/env python3
"""Aquarium auto top-up and skimmer sweep controller for Raspberry Pi Zero W.

Uses gpiozero with BCM pin numbering.

Usage:
    python3 topup.py [--config path/to/config.json]

Configuration
-------------
All GPIO assignments, electrical behaviour, and timings come from a JSON
config file (default: ``config.json`` next to this script). Every key is
required; nullable keys must be present with an explicit ``null``:

    water_sensor_gpio          BCM pin of the level sensor
    water_sensor_pull_up       true/false = internal pull resistor;
                               null = the sensor drives the line itself
    water_sensor_active_state  which pin level means "level full":
                               required true/false when pull_up is null,
                               must be null when pull_up is set (gpiozero
                               infers it from the pull direction)
    water_sensor_bounce_s      debounce time in seconds, or null for none
    topup_pump_gpio            BCM pin of the top-up pump relay
    topup_pump_active_high     true = relay energised by a HIGH level
    sweep_motor_gpio           BCM pin of the sweep motor relay
    sweep_motor_active_high    true = relay energised by a HIGH level
    topup_min_open_s           minimum pump run once started
    topup_min_close_s          minimum pause between top-ups (lockout)
    sweep_on_s / sweep_off_s   sweep motor cycle times

The program refuses to start on a missing or invalid config. The safe
initial output state (off) is intentionally not configurable.

Behaviour
---------
Top-up pump:
  * When the level sensor reports LOW water, the pump turns on.
  * The pump runs for at least topup_min_open_s, then keeps running until
    the sensor reports the level restored, at which point it turns off.
  * After turning off, the pump stays off for at least topup_min_close_s
    (lockout), then turns on again as soon as the sensor reports LOW water.
  * At start-up: pump turns on immediately if the level is low; otherwise
    the lockout period starts.

Skimmer sweep motor:
  * Off for sweep_off_s, on for sweep_on_s, repeating; starts with the
    off period.

Safety
------
  * Both outputs are initialised OFF and forced OFF on exit (SIGINT,
    SIGTERM, or an unexpected error), including after a partial start-up
    failure.
  * A stop request observed before an output activation prevents it; a
    stop landing in the same instant as an activation is bounded by the
    teardown, which joins the control threads and forces the outputs off
    afterwards.
  * During teardown the sensor callbacks are detached and controllers are
    stopped before the outputs are switched off and the devices closed,
    so a late sensor edge cannot re-energise an output. No teardown step
    can abort the remaining ones; any teardown failure (including a
    device close) is logged and fails the process.
  * If a control thread fails — including a failure to switch its own
    output off — the whole program stops with a non-zero exit code so a
    supervisor (e.g. systemd with Restart=on-failure) can restart it.
    A worker thread that fails to stop in time is also reported and
    produces a non-zero exit code.
  * There is NO maximum pump run time: once on, the pump runs until the
    sensor reports the level restored. A warning is logged when the
    minimum run time expires with the level still low.

Hardware assumptions (shipped configs)
--------------------------------------
  * Optical level sensor on GPIO 21 drives its output HIGH when dry and
    LOW when wet; wet means the level is FULL (pull_up null,
    active_state false).
  * Pump relay (GPIO 8) and sweep motor relay (GPIO 9) are active-high.
  * If the configured output pins are SPI pins (e.g. GPIO 8/9), the SPI
    interface must be disabled (raspi-config -> Interface Options).
"""

import argparse
import json
import logging
import math
import signal
import sys
import threading
from pathlib import Path

from gpiozero import DigitalInputDevice, DigitalOutputDevice

DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.json")

GPIO_KEYS = ("water_sensor_gpio", "topup_pump_gpio", "sweep_motor_gpio")
TIMING_KEYS = ("topup_min_open_s", "topup_min_close_s",
               "sweep_on_s", "sweep_off_s")
POLARITY_KEYS = ("topup_pump_active_high", "sweep_motor_active_high")

log = logging.getLogger("topup")


def load_config(path):
    """Read and validate settings from a JSON file (see module docstring
    for the schema). Raises ValueError on any problem."""
    try:
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
    except OSError as exc:
        raise ValueError(f"cannot read config {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in config {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise ValueError(f"config {path}: root must be a JSON object")

    def fail(key, requirement):
        raise ValueError(f"config {path}: '{key}' must be {requirement}, "
                         f"got {config.get(key)!r}")

    def is_number(value):
        return (isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value))

    for key in GPIO_KEYS:
        value = config.get(key)
        if (isinstance(value, bool) or not isinstance(value, int)
                or not 0 <= value <= 27):
            fail(key, "a BCM GPIO number from 0 to 27")
    if len({config[key] for key in GPIO_KEYS}) != len(GPIO_KEYS):
        raise ValueError(f"config {path}: water sensor, pump, and sweep "
                         "motor must use distinct GPIOs")

    for key in TIMING_KEYS:
        if not is_number(config.get(key)) or config[key] <= 0:
            fail(key, "a positive number of seconds")

    for key in POLARITY_KEYS:
        if not isinstance(config.get(key), bool):
            fail(key, "true or false")

    for key in ("water_sensor_pull_up", "water_sensor_active_state",
                "water_sensor_bounce_s"):
        if key not in config:
            raise ValueError(f"config {path}: missing required key '{key}' "
                             "(nullable keys need an explicit null)")

    pull_up = config.get("water_sensor_pull_up")
    active_state = config.get("water_sensor_active_state")
    if pull_up is not None and not isinstance(pull_up, bool):
        fail("water_sensor_pull_up", "true, false, or null")
    if active_state is not None and not isinstance(active_state, bool):
        fail("water_sensor_active_state", "true, false, or null")
    if pull_up is None and active_state is None:
        raise ValueError(
            f"config {path}: 'water_sensor_active_state' must be true or "
            "false when 'water_sensor_pull_up' is null")
    if pull_up is not None and active_state is not None:
        raise ValueError(
            f"config {path}: 'water_sensor_active_state' must be null when "
            "'water_sensor_pull_up' is set")

    bounce = config.get("water_sensor_bounce_s")
    if bounce is not None and (not is_number(bounce) or bounce <= 0):
        fail("water_sensor_bounce_s", "a positive number of seconds or null")

    return config


class TopUp:
    """Keeps the water level topped up.

    Two states:
      OPEN  (pump on):  after ``min_open`` s, off as soon as level is full.
      CLOSE (pump off): after ``min_close`` s, on as soon as level is low.

    ``sensor`` must expose ``is_active`` (True = level full) and the
    ``when_activated``/``when_deactivated`` callbacks; ``pump`` must
    expose ``on()``/``off()``. The thread blocks on events between
    transitions and consumes negligible CPU while idle. A stop event
    observed before the thread starts prevents the pump from ever being
    switched on.
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
        """Switch the pump on unless a stop was requested; returns whether
        the pump is now on. A stop can still land between the check and
        on(); run()'s teardown bounds that race by joining this thread and
        forcing the pump off afterwards."""
        if self._stop.is_set():
            return False
        self._pump.on()
        log.info("top-up: water low, pump ON (min run %gs)", self._min_open)
        return True

    def _close(self):
        self._pump.off()
        log.info("top-up: level restored, pump OFF (lockout %gs)",
                 self._min_close)

    def run(self):
        try:
            if self._stop.is_set():
                return
            if self._sensor.is_active:
                self._pump.off()
                pump_open = False
                log.info("top-up: level full at start, pump off (lockout %gs)",
                         self._min_close)
            else:
                pump_open = self._open()
            while not self._stop.is_set():
                if pump_open:
                    self._stop.wait(self._min_open)
                    if not self._stop.is_set() and not self._sensor.is_active:
                        log.warning(
                            "top-up: pump has run for %gs and the water "
                            "level is still low; pump stays ON (there is "
                            "no maximum run time)", self._min_open)
                    self._wait_for_level(full=True)
                    if self._stop.is_set():
                        break
                    self._close()
                    pump_open = False
                else:
                    self._stop.wait(self._min_close)
                    self._wait_for_level(full=False)
                    pump_open = self._open()
                    if not pump_open:
                        break
        except Exception:
            self.crashed = True
            log.exception("top-up: control thread failed")
        finally:
            try:
                self._pump.off()
            except Exception:
                self.crashed = True
                log.exception("top-up: could not switch the pump off")
            finally:
                self._stop.set()  # fail loud even if the off() above raised
                log.info("top-up: stopped")


class SkimmerSweep:
    """Runs the sweep motor for ``on_time`` s every ``off_time`` s,
    starting with the off period. A stop event set before the thread
    starts prevents the motor from ever being switched on."""

    def __init__(self, motor, stop, on_time, off_time):
        self._motor = motor
        self._stop = stop
        self._on_time = on_time
        self._off_time = off_time
        self.crashed = False

    def run(self):
        try:
            while not self._stop.wait(self._off_time):
                # Re-check: a stop can race the wait timeout. One can still
                # land between this check and on(); run()'s teardown bounds
                # that by joining this thread and forcing the motor off.
                if self._stop.is_set():
                    break
                self._motor.on()
                log.info("sweep: motor ON for %gs", self._on_time)
                self._stop.wait(self._on_time)
                self._motor.off()
                log.info("sweep: motor OFF for %gs", self._off_time)
        except Exception:
            self.crashed = True
            log.exception("sweep: control thread failed")
        finally:
            try:
                self._motor.off()
            except Exception:
                self.crashed = True
                log.exception("sweep: could not switch the motor off")
            finally:
                self._stop.set()  # fail loud even if the off() above raised
                log.info("sweep: stopped")


def run(config, stop, wakers=None):
    """Create the GPIO devices and controllers, run until ``stop`` is
    set, then tear everything down in a safe order. Returns the process
    exit code. If ``wakers`` is given, the top-up waker is appended to it
    so a signal handler can interrupt a pending level wait."""
    devices = []   # every successfully created device, for closing
    outputs = []   # pump and motor, for forcing off
    threads = []
    controllers = []
    sensor = None
    failed = False
    try:
        sensor = DigitalInputDevice(
            config["water_sensor_gpio"],
            pull_up=config["water_sensor_pull_up"],
            active_state=config["water_sensor_active_state"],
            bounce_time=config["water_sensor_bounce_s"])
        devices.append(sensor)
        pump = DigitalOutputDevice(
            config["topup_pump_gpio"],
            active_high=config["topup_pump_active_high"],
            initial_value=False)  # never energised during setup
        devices.append(pump)
        outputs.append(pump)
        motor = DigitalOutputDevice(
            config["sweep_motor_gpio"],
            active_high=config["sweep_motor_active_high"],
            initial_value=False)
        devices.append(motor)
        outputs.append(motor)

        topup = TopUp(sensor, pump, stop,
                      min_open=config["topup_min_open_s"],
                      min_close=config["topup_min_close_s"])
        sweep = SkimmerSweep(motor, stop,
                             on_time=config["sweep_on_s"],
                             off_time=config["sweep_off_s"])
        controllers = [topup, sweep]
        if wakers is not None:
            wakers.append(topup.wake)

        log.info("starting: sensor GPIO%d, pump GPIO%d, sweep motor GPIO%d",
                 config["water_sensor_gpio"], config["topup_pump_gpio"],
                 config["sweep_motor_gpio"])
        for target, name in ((topup.run, "topup"), (sweep.run, "sweep")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            threads.append(thread)
        stop.wait()
    except Exception:
        log.exception("unexpected fatal error; shutting down outputs")
        failed = True
    finally:
        def teardown_step(description, action):
            """One teardown step must never abort the remaining ones; any
            failure is logged and fails the process instead."""
            nonlocal failed
            try:
                action()
            except Exception:
                log.exception("teardown: %s failed", description)
                failed = True

        stop.set()
        for controller in controllers:
            if isinstance(controller, TopUp):
                teardown_step("waking the top-up thread", controller.wake)
        for thread in threads:
            teardown_step("joining the %s thread" % thread.name,
                          lambda t=thread: t.join(timeout=5))
            if thread.is_alive():
                log.error("%s thread did not stop within 5 seconds",
                          thread.name)
                failed = True

        # Detach callbacks and only then touch the outputs, so a late
        # sensor edge cannot re-enter the top-up logic during teardown.
        def detach_callbacks():
            sensor.when_activated = None
            sensor.when_deactivated = None

        if sensor is not None:
            teardown_step("detaching sensor callbacks", detach_callbacks)
        for output in outputs:
            teardown_step("switching an output off", output.off)
        for device in devices:
            teardown_step("closing a GPIO device", device.close)
        log.info("outputs off, exiting")
    failed = failed or any(c.crashed for c in controllers)
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Aquarium top-up and skimmer sweep controller")
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH,
        help=f"JSON configuration file (default: {DEFAULT_CONFIG_PATH})")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config(args.config)
    except ValueError as exc:
        log.error("%s", exc)
        return 1

    stop = threading.Event()
    wakers = []

    def handle_signal(signum, frame):
        log.info("received %s, shutting down", signal.Signals(signum).name)
        stop.set()
        for wake in wakers:
            wake()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    return run(config, stop, wakers)


if __name__ == "__main__":
    sys.exit(main())
