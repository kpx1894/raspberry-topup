"""Test suite for topup.py — no GPIO hardware required.

Controller behaviour and safety paths are tested with in-process fake
devices; the full run() lifecycle is tested against gpiozero's mock pin
factory. Timings are scaled down to fractions of a second.

    python3 test_topup.py
"""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

from gpiozero import Device
from gpiozero.pins.mock import MockFactory

import topup as m

TIMING_SLACK = 0.03


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class FakeSensor:
    """Level sensor stand-in: is_active True means the level is full."""

    def __init__(self, active=False):
        self.is_active = active
        self.when_activated = None
        self.when_deactivated = None

    def set_active(self, active):
        self.is_active = active
        callback = self.when_activated if active else self.when_deactivated
        if callback is not None:
            callback()


class FakeOutput:
    """Pump/motor stand-in recording timestamped transitions; can be told
    to raise from on() and/or off()."""

    def __init__(self, fail_on=()):
        self.events = []  # (monotonic timestamp, "on" | "off")
        self.fail_on = set(fail_on)

    def on(self):
        if "on" in self.fail_on:
            raise RuntimeError("injected on() failure")
        self.events.append((time.monotonic(), "on"))

    def off(self):
        if "off" in self.fail_on:
            raise RuntimeError("injected off() failure")
        self.events.append((time.monotonic(), "off"))

    @property
    def is_active(self):
        return bool(self.events) and self.events[-1][1] == "on"

    def switched_on(self):
        return any(state == "on" for _, state in self.events)


class ControllerTestCase(unittest.TestCase):
    """Shared thread management: every started controller thread is
    stopped and joined even when an assertion fails mid-test."""

    def start_controller(self, controller, stop):
        thread = threading.Thread(target=controller.run, daemon=True)
        thread.start()

        def cleanup():
            stop.set()
            if isinstance(controller, m.TopUp):
                controller.wake()
            thread.join(timeout=2)

        self.addCleanup(cleanup)
        return thread


class TopUpBehaviourTests(ControllerTestCase):

    def test_startup_full_keeps_pump_off_and_starts_lockout(self):
        sensor = FakeSensor(active=True)
        pump = FakeOutput()
        stop = threading.Event()
        topup = m.TopUp(sensor, pump, stop, min_open=0.05, min_close=0.3)
        started_at = time.monotonic()
        self.start_controller(topup, stop)

        time.sleep(0.05)
        self.assertFalse(pump.switched_on())
        # The initial transition must establish the off state explicitly,
        # not assume the injected pump is already off.
        self.assertEqual(pump.events[0][1], "off")

        # Water drops during the lockout: pump must stay off...
        sensor.set_active(False)
        time.sleep(0.1)
        self.assertFalse(pump.switched_on())
        # ...and turn on once the lockout expires.
        self.assertTrue(wait_for(pump.switched_on, timeout=0.5))
        on_at = next(t for t, state in pump.events if state == "on")
        self.assertGreaterEqual(on_at - started_at, 0.3 - TIMING_SLACK)

    def test_startup_low_starts_pump_immediately(self):
        sensor = FakeSensor(active=False)
        pump = FakeOutput()
        stop = threading.Event()
        topup = m.TopUp(sensor, pump, stop, min_open=5, min_close=5)
        self.start_controller(topup, stop)
        self.assertTrue(wait_for(lambda: pump.is_active, timeout=0.5))

    def test_minimum_run_holds_pump_on_despite_full_level(self):
        sensor = FakeSensor(active=False)
        pump = FakeOutput()
        stop = threading.Event()
        topup = m.TopUp(sensor, pump, stop, min_open=0.2, min_close=5)
        self.start_controller(topup, stop)
        self.assertTrue(wait_for(lambda: pump.is_active, timeout=0.5))
        on_at = pump.events[-1][0]

        # Level restored immediately: pump must keep running.
        sensor.set_active(True)
        time.sleep(0.05)
        self.assertTrue(pump.is_active)
        self.assertTrue(wait_for(lambda: not pump.is_active, timeout=0.5))
        off_at = pump.events[-1][0]
        self.assertGreaterEqual(off_at - on_at, 0.2 - TIMING_SLACK)

    def test_shutdown_while_pumping_turns_pump_off(self):
        sensor = FakeSensor(active=False)
        pump = FakeOutput()
        stop = threading.Event()
        topup = m.TopUp(sensor, pump, stop, min_open=5, min_close=5)
        thread = self.start_controller(topup, stop)
        self.assertTrue(wait_for(lambda: pump.is_active, timeout=0.5))

        stop.set()
        topup.wake()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(pump.is_active)
        self.assertFalse(topup.crashed)


class TopUpSafetyTests(ControllerTestCase):

    def test_preset_stop_never_switches_pump_on(self):
        sensor = FakeSensor(active=False)  # water low: would normally pump
        pump = FakeOutput()
        stop = threading.Event()
        stop.set()  # shutdown requested before the thread starts
        topup = m.TopUp(sensor, pump, stop, min_open=5, min_close=5)
        thread = self.start_controller(topup, stop)
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(pump.switched_on())

    def test_pump_failure_sets_stop_even_if_off_also_fails(self):
        sensor = FakeSensor(active=False)
        pump = FakeOutput(fail_on=("on", "off"))
        stop = threading.Event()
        topup = m.TopUp(sensor, pump, stop, min_open=5, min_close=5)
        thread = self.start_controller(topup, stop)
        # on() raises -> crash; off() in the finally also raises; the
        # shared stop event must still be set so main() cannot hang.
        self.assertTrue(wait_for(stop.is_set, timeout=1.0))
        thread.join(timeout=2)
        self.assertTrue(topup.crashed)

    def test_off_failure_during_shutdown_is_reported(self):
        sensor = FakeSensor(active=True)
        pump = FakeOutput(fail_on=("off",))
        stop = threading.Event()
        topup = m.TopUp(sensor, pump, stop, min_open=5, min_close=5)
        thread = self.start_controller(topup, stop)
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(topup.crashed)
        self.assertTrue(stop.is_set())

    def test_warning_logged_when_minimum_run_expires_with_level_low(self):
        sensor = FakeSensor(active=False)
        pump = FakeOutput()
        stop = threading.Event()
        topup = m.TopUp(sensor, pump, stop, min_open=0.05, min_close=5)
        with self.assertLogs("topup", level="WARNING") as captured:
            self.start_controller(topup, stop)
            self.assertTrue(wait_for(lambda: pump.is_active, timeout=0.5))
            time.sleep(0.15)  # min run expires, level still low
            sensor.set_active(True)
            self.assertTrue(wait_for(lambda: not pump.is_active, timeout=0.5))
        self.assertTrue(any("still low" in line for line in captured.output))


class SkimmerSweepTests(ControllerTestCase):

    def test_cycle_durations(self):
        motor = FakeOutput()
        stop = threading.Event()
        sweep = m.SkimmerSweep(motor, stop, on_time=0.15, off_time=0.25)
        started_at = time.monotonic()
        self.start_controller(sweep, stop)

        # Starts with the off period: the first ON must not come early.
        self.assertTrue(wait_for(lambda: motor.is_active, timeout=1.0))
        on_at = motor.events[-1][0]
        self.assertGreaterEqual(on_at - started_at, 0.25 - TIMING_SLACK)

        # The ON phase must last at least on_time.
        self.assertTrue(wait_for(lambda: not motor.is_active, timeout=1.0))
        off_at = motor.events[-1][0]
        self.assertGreaterEqual(off_at - on_at, 0.15 - TIMING_SLACK)

    def test_shutdown_turns_motor_off(self):
        motor = FakeOutput()
        stop = threading.Event()
        sweep = m.SkimmerSweep(motor, stop, on_time=5, off_time=0.05)
        thread = self.start_controller(sweep, stop)
        self.assertTrue(wait_for(lambda: motor.is_active, timeout=0.5))
        stop.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(motor.is_active)
        self.assertFalse(sweep.crashed)

    def test_motor_failure_sets_stop(self):
        motor = FakeOutput(fail_on=("on",))
        stop = threading.Event()
        sweep = m.SkimmerSweep(motor, stop, on_time=5, off_time=0.05)
        self.start_controller(sweep, stop)
        self.assertTrue(wait_for(stop.is_set, timeout=1.0))
        self.assertTrue(sweep.crashed)


def make_config(**overrides):
    config = {
        "water_sensor_gpio": 21,
        "water_sensor_pull_up": None,
        "water_sensor_active_state": False,
        "water_sensor_bounce_s": None,
        "topup_pump_gpio": 8,
        "topup_pump_active_high": True,
        "sweep_motor_gpio": 9,
        "sweep_motor_active_high": True,
        "topup_min_open_s": 0.1,
        "topup_min_close_s": 0.2,
        "sweep_on_s": 0.1,
        "sweep_off_s": 0.2,
    }
    config.update(overrides)
    return config


class LifecycleTests(unittest.TestCase):
    """Drive the module-level run() end to end on mock pins."""

    def setUp(self):
        Device.pin_factory = MockFactory()
        self.addCleanup(Device.pin_factory.reset)

    def test_normal_start_and_stop_leaves_devices_off_and_closed(self):
        config = make_config()
        stop = threading.Event()
        result = []
        runner = threading.Thread(
            target=lambda: result.append(m.run(config, stop)), daemon=True)
        runner.start()
        time.sleep(0.1)
        stop.set()
        runner.join(timeout=5)
        self.assertFalse(runner.is_alive())
        self.assertEqual(result, [0])
        # Output pins are deasserted and the devices were closed: the
        # pins can be re-acquired and read low.
        for pin_number in (8, 9):
            pin = Device.pin_factory.pin(pin_number)
            self.assertEqual(pin.state, 0)

    def test_preset_stop_exits_cleanly_without_energising_outputs(self):
        config = make_config()
        stop = threading.Event()
        stop.set()
        self.assertEqual(m.run(config, stop), 0)
        for pin_number in (8, 9):
            pin = Device.pin_factory.pin(pin_number)
            self.assertEqual(pin.state, 0)

    def test_device_close_failure_fails_the_process(self):
        config = make_config()
        real_input = m.DigitalInputDevice

        def sensor_with_bad_close(*args, **kwargs):
            device = real_input(*args, **kwargs)
            original_close = device.close

            def bad_close():
                original_close()
                device.close = original_close  # fail once, not again on GC
                raise RuntimeError("injected close failure")

            device.close = bad_close
            return device

        stop = threading.Event()
        stop.set()
        with mock.patch.object(m, "DigitalInputDevice", sensor_with_bad_close):
            self.assertEqual(m.run(config, stop), 1)
        # The failure must not have aborted the rest of the teardown.
        for pin_number in (8, 9):
            self.assertEqual(Device.pin_factory.pin(pin_number).state, 0)

    def test_teardown_continues_when_wake_fails(self):
        class BadWake(m.TopUp):
            def wake(self):
                raise RuntimeError("injected wake failure")

        config = make_config()
        stop = threading.Event()
        stop.set()  # controllers exit immediately; only teardown matters
        with mock.patch.object(m, "TopUp", BadWake):
            self.assertEqual(m.run(config, stop), 1)
        # Outputs were still switched off despite the wake failure.
        for pin_number in (8, 9):
            self.assertEqual(Device.pin_factory.pin(pin_number).state, 0)

    def test_partial_init_failure_cleans_up_created_devices(self):
        config = make_config()
        created = []
        real_output = m.DigitalOutputDevice

        def flaky_output(*args, **kwargs):
            if created:
                raise RuntimeError("injected device failure")
            device = real_output(*args, **kwargs)
            created.append(device)
            return device

        with mock.patch.object(m, "DigitalOutputDevice", flaky_output):
            self.assertEqual(m.run(config, threading.Event()), 1)
        # The one output that was created must be off and closed.
        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].closed)


class ConfigTests(unittest.TestCase):

    def load(self, config):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            return m.load_config(path)

    def assert_rejected(self, config, expected_fragment):
        with self.assertRaises(ValueError) as ctx:
            self.load(config)
        self.assertIn(expected_fragment, str(ctx.exception))

    def test_shipped_live_config_is_valid(self):
        config = m.load_config(Path(__file__).with_name("config-live.json"))
        self.assertEqual(config["water_sensor_gpio"], 21)
        self.assertEqual(config["topup_min_close_s"], 14400)

    def test_shipped_test_config_is_valid(self):
        config = m.load_config(Path(__file__).with_name("config-test.json"))
        self.assertEqual(config["water_sensor_gpio"], 21)
        self.assertEqual(config["topup_min_close_s"], 30)

    def test_valid_config_passes(self):
        self.assertEqual(self.load(make_config())["topup_pump_gpio"], 8)

    def test_missing_key_rejected(self):
        config = make_config()
        del config["sweep_on_s"]
        self.assert_rejected(config, "sweep_on_s")

    def test_missing_nullable_key_rejected(self):
        config = make_config()
        del config["water_sensor_bounce_s"]
        self.assert_rejected(config, "water_sensor_bounce_s")

    def test_missing_pull_up_rejected_even_with_active_state(self):
        config = make_config()  # active_state False would otherwise satisfy
        del config["water_sensor_pull_up"]
        self.assert_rejected(config, "water_sensor_pull_up")

    def test_negative_timing_rejected(self):
        self.assert_rejected(make_config(topup_min_open_s=-5),
                             "topup_min_open_s")

    def test_nan_timing_rejected(self):
        self.assert_rejected(make_config(sweep_on_s=float("nan")),
                             "sweep_on_s")

    def test_infinite_timing_rejected(self):
        self.assert_rejected(make_config(topup_min_close_s=float("inf")),
                             "topup_min_close_s")

    def test_nan_bounce_rejected(self):
        self.assert_rejected(make_config(water_sensor_bounce_s=float("nan")),
                             "water_sensor_bounce_s")

    def test_fractional_gpio_rejected(self):
        self.assert_rejected(make_config(topup_pump_gpio=8.5),
                             "topup_pump_gpio")

    def test_out_of_range_gpio_rejected(self):
        self.assert_rejected(make_config(sweep_motor_gpio=100),
                             "sweep_motor_gpio")

    def test_boolean_gpio_rejected(self):
        self.assert_rejected(make_config(water_sensor_gpio=True),
                             "water_sensor_gpio")

    def test_duplicate_gpios_rejected(self):
        self.assert_rejected(make_config(topup_pump_gpio=21),
                             "distinct")

    def test_non_boolean_polarity_rejected(self):
        self.assert_rejected(make_config(topup_pump_active_high=1),
                             "topup_pump_active_high")

    def test_pull_up_with_active_state_rejected(self):
        self.assert_rejected(
            make_config(water_sensor_pull_up=True,
                        water_sensor_active_state=False),
            "must be null")

    def test_null_pull_up_requires_active_state(self):
        self.assert_rejected(
            make_config(water_sensor_pull_up=None,
                        water_sensor_active_state=None),
            "must be true or false")

    def test_pull_up_with_null_active_state_accepted(self):
        config = self.load(make_config(water_sensor_pull_up=True,
                                       water_sensor_active_state=None))
        self.assertIs(config["water_sensor_pull_up"], True)

    def test_negative_bounce_rejected(self):
        self.assert_rejected(make_config(water_sensor_bounce_s=-0.1),
                             "water_sensor_bounce_s")

    def test_positive_bounce_accepted(self):
        config = self.load(make_config(water_sensor_bounce_s=0.1))
        self.assertEqual(config["water_sensor_bounce_s"], 0.1)

    def test_missing_file_rejected_with_path_in_message(self):
        with self.assertRaises(ValueError) as ctx:
            m.load_config(Path("does-not-exist") / "config.json")
        self.assertIn("does-not-exist", str(ctx.exception))

    def test_malformed_json_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                m.load_config(path)
            self.assertIn("invalid JSON", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
