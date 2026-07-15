# Aquarium Top-Up and Skimmer Sweep Controller

A lightweight Python daemon for a Raspberry Pi Zero W that keeps an
aquarium's water level topped up and periodically runs a skimmer sweep
motor. Built on [gpiozero](https://gpiozero.readthedocs.io/) with BCM
pin numbering. Designed to share the Pi with other applications: it
consumes essentially zero CPU while idle and has no dependencies beyond
gpiozero.

## Functionality

### Top-up pump

An optical level sensor watches the water line. The pump follows a
two-state cycle:

- **Water low → pump on.** Once started, the pump runs for at least
  `topup_min_open_s` (default 30 s), then keeps running until the sensor
  reports the level restored, at which point it turns off.
- **Level restored → lockout.** After turning off, the pump stays off
  for at least `topup_min_close_s` (default 4 h) regardless of what the
  sensor reports. When the lockout expires, the pump turns on again as
  soon as (or if) the sensor reports low water.

The minimum run time stops sensor chatter at the water line from
short-cycling the pump; the lockout bounds how often water can be added,
so a misbehaving sensor cannot trigger continuous back-to-back top-ups.

At start-up: if the level is low the pump turns on immediately;
otherwise the lockout period begins.

### Skimmer sweep motor

A simple repeating cycle, starting with the off period: off for
`sweep_off_s` (default 4 h), then on for `sweep_on_s` (default 60 s).

## Hardware

| Signal | Default GPIO (BCM) | Notes |
|---|---|---|
| Water level sensor | 21 | Optical sensor, drives its output: HIGH = dry, LOW = wet (level full). No internal pull resistor is used. |
| Top-up pump relay | 8 | Active-high |
| Sweep motor relay | 9 | Active-high |

- The sensor output must be a **3.3 V** signal — a 5 V-powered sensor
  that drives 5 V on its signal pin will damage the Pi's GPIO.
- GPIO 8/9 are the SPI CE0/MISO pins: the SPI interface must be disabled
  (`raspi-config` → Interface Options → SPI → No), or different pins
  configured.

## Configuration

All pins and timings live in `config.json` (no values are hardcoded).
By default the file is loaded from the script's directory; an alternate
path can be given as the first argument.

```json
{
    "water_sensor_gpio": 21,
    "topup_pump_gpio": 8,
    "sweep_motor_gpio": 9,
    "topup_min_open_s": 30,
    "topup_min_close_s": 14400,
    "sweep_on_s": 60,
    "sweep_off_s": 14400
}
```

All keys are required and must be positive numbers; times are in
seconds. The program refuses to start (exit code 1, clear log message)
on a missing or invalid config — it never falls back to silent defaults.

## Installation and usage

```bash
sudo apt install python3-gpiozero    # usually preinstalled on Raspberry Pi OS

python3 topup.py                     # config.json next to the script
python3 topup.py /etc/topup.json     # explicit config path
```

Stop with Ctrl+C (or SIGTERM); both outputs are switched off before the
process exits.

## Architecture

Two small state-machine classes, each running in its own thread,
coordinated by a single shared `threading.Event` for shutdown:

- **`TopUp`** owns the sensor and the pump. Timed phases (minimum run,
  lockout) block on `stop.wait(seconds)`; level waits block on a
  `threading.Condition` that is notified by gpiozero's
  `when_activated`/`when_deactivated` edge callbacks.
- **`SkimmerSweep`** owns the motor and is just two alternating
  `stop.wait()` calls.
- **`main()`** loads the config, creates the GPIO devices, installs
  SIGINT/SIGTERM handlers, starts the threads and blocks until shutdown.

### Design decisions

- **No polling loop.** Every thread is blocked on an event or condition
  between transitions; there are no periodic wake-ups, so idle CPU use is
  zero — important on a Pi Zero W shared with other applications.
- **Condition + callbacks instead of `sensor.wait_for_active()`.**
  gpiozero's blocking waits cannot be interrupted by an external stop
  event, which would make shutdown non-deterministic while the pump is
  running. Waiting on a condition whose predicate also checks the stop
  event keeps every wait interruptible.
- **Threads instead of asyncio.** Two independent linear cycles map
  naturally onto two threads reading top to bottom; an event loop would
  add complexity without saving resources at this scale.
- **Dependency injection for testability.** The classes accept any
  objects with the right interface (`is_active`, `on()`/`off()`,
  callbacks) and take their timings as constructor arguments, so the
  full state machines run against simulated pins with millisecond
  timings in the test suite.

## Safety features

- **Safe initial state:** both outputs are created off; the pump and
  motor are never energised before the state machines decide to.
- **Safe shutdown:** on SIGINT, SIGTERM or an unexpected exception, both
  outputs are forced off before the process exits (`finally` block in
  `main()`, plus each thread switches its own output off on exit).
- **Fail loud:** if a control thread crashes, it turns its own output
  off, logs the traceback and stops the entire program with a non-zero
  exit code, so a supervisor restarts it — the program never keeps
  running half-broken.
- **Bounded top-up frequency:** the lockout guarantees a minimum pause
  between top-ups even with a faulty sensor.
- **Config validation:** missing or non-positive values abort start-up
  before any GPIO device is created.
- **Known limitation:** there is **no maximum pump run time**. If the
  sensor sticks at "dry" while the pump is running, the pump runs until
  the sensor recovers. Size the top-up reservoir so that emptying it
  completely cannot overflow the aquarium, or add a max-run cutoff in
  `TopUp` if that guarantee is needed.

## Testing

`test_topup.py` exercises the real state-machine code against
[gpiozero's mock pin factory](https://gpiozero.readthedocs.io/en/stable/api_pins.html#mock-pins)
— no hardware required — with timings scaled down to milliseconds. It
runs on any machine with Python 3 and gpiozero installed:

```bash
python3 test_topup.py
```

Covered scenarios:

- start-up with the level full (pump stays off, lockout begins) and with
  the level low (pump turns on immediately);
- the pump is held off during the lockout even when the water is low,
  and turns on as soon as the lockout expires;
- the pump keeps running for the minimum run time even if the level is
  restored instantly, and turns off once both conditions are met;
- the sweep motor starts with its off period and honours both the off
  and on durations;
- shutdown while the pump is running forces all outputs off and both
  threads exit promptly;
- config loading: the shipped `config.json` validates; a missing key, a
  negative value, and a missing file are all rejected.

The test prints one PASS/FAIL line per check and exits non-zero on any
failure.

## Running as a service

Create `/etc/systemd/system/topup.service`:

```ini
[Unit]
Description=Aquarium top-up and skimmer sweep controller

[Service]
ExecStart=/usr/bin/python3 /home/pi/topup/topup.py
User=pi
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now topup
journalctl -u topup -f        # follow the logs
```

The `User=` account must be in the `gpio` group (the default `pi` user
is). `systemctl stop` sends SIGTERM, which the program handles by
switching both outputs off before exiting; `Restart=on-failure` pairs
with the non-zero exit code the program uses when a control thread
crashes or the config is invalid, while a deliberate `systemctl stop`
stays stopped.
