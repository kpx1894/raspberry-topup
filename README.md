# Aquarium Top-Up and Skimmer Sweep Controller

A lightweight Python daemon for a Raspberry Pi Zero W that keeps an
aquarium's water level topped up and periodically runs a skimmer sweep
motor. Built on [gpiozero](https://gpiozero.readthedocs.io/) with BCM
pin numbering. Designed to share the Pi with other applications: it
consumes negligible CPU while idle and has no dependencies beyond
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

| Signal | Default GPIO (BCM) | Default electrical behaviour |
|---|---|---|
| Water level sensor | 21 | Optical sensor drives the line: HIGH = dry, LOW = wet (level full). No internal pull resistor. |
| Top-up pump relay | 16 | Active-high |
| Sweep motor relay | 11 | Active-high |

### Relay board pin map

The relay board has 8 sockets in 3 rows (2 unused). Current wiring:

| GPIO (BCM) | Relay | Socket | Use |
|---|---|---|---|
| 8 | 1 | upper row, right | **Inoperative** — relay burned out; socket wired permanently on |
| 9 | 2 | upper row, middle | unused |
| 10 | 3 | upper row, left | unused |
| 11 | 4 | middle row, right | Skimmer sweep motor |
| 12 | 5 | middle row, middle | unused |
| 13 | 6 | middle row, left | unused |
| 16 | 7 | bottom row, right | Top-up valve/pump |
| 17 | 8 | bottom row, middle | unused |
| 21 | — | — | Optical water sensor: no internal pull resistor (`water_sensor_pull_up: null`); output LOW = wet (level full), HIGH = dry (`water_sensor_active_state: false`) |

- The pump and motor must be driven through suitable driver electronics
  (relay modules or MOSFET circuits) — never directly from a GPIO pin.
- Power the sensor from the Pi's **3.3 V** rail so its output can never
  exceed 3.3 V — a sensor that drives more than 3.3 V on its signal pin
  stresses or damages the Pi's GPIO. For bare LED-plus-phototransistor
  modules (no on-board comparator), measure the signal pin with the tip
  dry after switching to 3.3 V: it should stay comfortably above
  ~2.5 V. If it sits near the Pi's ~1.6 V input threshold instead,
  lower the IR LED's series resistor to restore ~10 mA of LED current
  (about 200 Ω at 3.3 V, e.g. a 470 Ω soldered in parallel with a
  factory 380 Ω).
- GPIO 8/9/10/11 are the SPI CE0/MISO/MOSI/SCLK pins — the sweep motor
  relay (GPIO 11) sits on one of them, so the SPI interface must stay
  disabled (`raspi-config` → Interface Options → SPI → No), or a
  different pin configured.

## Configuration

All pins, electrical behaviour, and timings live in a JSON config file
(no values are hardcoded). The repository ships two:

- `config-live.json` — production timings (shown below);
- `config-test.json` — short cycles for live testing (see below).

The program reads `config.json`, which is not in the repository: create
it as a symlink to whichever config should be active. By default it is
loaded from the script's directory; use `--config` for an alternate
path.

```json
{
    "water_sensor_gpio": 21,
    "water_sensor_pull_up": null,
    "water_sensor_active_state": false,
    "water_sensor_bounce_s": null,
    "topup_pump_gpio": 16,
    "topup_pump_active_high": true,
    "sweep_motor_gpio": 11,
    "sweep_motor_active_high": true,
    "topup_min_open_s": 30,
    "topup_min_close_s": 14400,
    "sweep_on_s": 60,
    "sweep_off_s": 14400
}
```

Every key is required (nullable keys must be present with an explicit
`null`); times are in seconds. Validation rules:

- GPIO keys must be distinct integers in the BCM range 0–27.
- Timing keys must be positive numbers.
- `water_sensor_pull_up`: `true`/`false` selects the Pi's internal pull
  resistor; `null` means the sensor drives the line itself.
- `water_sensor_active_state` says which pin level means "level full".
  It must be `true` or `false` when `water_sensor_pull_up` is `null`,
  and must be `null` when a pull resistor is selected (gpiozero infers
  it from the pull direction).
- `water_sensor_bounce_s`: software debounce in seconds, or `null` for
  none.

> **Warning:** `topup_pump_active_high` and `sweep_motor_active_high`
> describe the relay wiring. Setting one wrongly **inverts that
> output** — "off" energises the load — while every log message still
> reads correctly. Verify against the actual relay board before
> unattended operation. The safe initial output state (off) is
> intentionally not configurable.

The program refuses to start (exit code 1, clear log message) on a
missing or invalid config — it never falls back to silent defaults, and
no GPIO device is opened before the config has validated.

### Live-test configuration

`config-test.json` has the same pins and electrical settings as
`config-live.json` but short cycles — pump minimum run 5 s, lockout
30 s, sweep 5 s on / 30 s off — so every state transition can be
observed within a minute instead of hours. It drives the real outputs:
the pump and motor genuinely switch.

Testing procedure — the active config is selected by re-pointing the
`config.json` symlink and restarting the service:

1. Switch to the test timings and watch the log:

   ```bash
   cd ~/topup
   ln -sf config-test.json config.json
   sudo systemctl restart topup
   journalctl -u topup -f
   ```

2. What to expect in the log: the initial sensor decision at startup
   (pump on if the level is low); the min-run warning 5 s later if the
   level is still low; the sweep motor on at the 30 s mark and off again
   5 s later, repeating every 30 s. With the level full, the pump comes
   on within 30 s of a "water low" edge (test lockout).

3. Switch back to the production config when done — the test timings
   must not be left in place unattended (30 s lockout defeats the
   bounded top-up frequency safety):

   ```bash
   ln -sf config-live.json config.json
   sudo systemctl restart topup
   systemctl is-active topup      # expect: active
   ```

The symlink swap is atomic for the service: the config is read once at
startup, so timings only change on restart.

## Installation and usage

```bash
sudo apt install git python3-gpiozero   # both usually preinstalled on Raspberry Pi OS
git clone https://github.com/kpx1894/raspberry-topup.git ~/topup
cd ~/topup
ln -s config-live.json config.json   # select the active config (see Configuration)
python3 test_topup.py                # post-install check; runs on mock pins, no GPIO touched

python3 topup.py                     # config.json next to the script
python3 topup.py --config /etc/topup.json
python3 topup.py --help
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
- **`run(config, stop)`** creates the GPIO devices, starts the threads,
  blocks until shutdown, and tears everything down in a safe order.
- **`main(argv)`** parses arguments, loads the config, and installs
  SIGINT/SIGTERM handlers around `run()`.

### Design decisions

- **No polling loop.** Every thread is blocked on an event or condition
  between transitions; there are no periodic wake-ups, so idle CPU use
  is negligible — important on a Pi Zero W shared with other
  applications.
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
  full state machines run against fake devices with millisecond timings
  in the test suite; `run()` is separated from `main()` so the whole
  lifecycle is testable on mock pins.

## Safety features

- **Safe initial state:** both outputs are created off
  (`initial_value=False`, not configurable); the pump and motor are
  never energised before the state machines decide to.
- **Safe shutdown:** on SIGINT, SIGTERM or an unexpected exception —
  including a failure part-way through device creation — every
  successfully created output is forced off and every device is
  explicitly closed.
- **Ordered teardown:** controllers are stopped and the sensor callbacks
  detached *before* the outputs are switched off and closed, so a late
  sensor edge cannot re-energise an output during teardown.
- **Stop-before-start guarantee:** a shutdown requested before a control
  thread begins running prevents any output from being switched on at
  all.
- **Fail loud:** if a control thread crashes — even if switching its own
  output off then also fails — the shared stop event is still set, the
  traceback is logged, and the program exits non-zero so a supervisor
  restarts it. A worker thread that fails to stop within 5 seconds is
  likewise reported and produces a non-zero exit code.
- **Bounded top-up frequency:** the lockout guarantees a minimum pause
  between top-ups even with a faulty sensor.
- **Config validation:** schema, ranges, pin uniqueness, and the
  pull-up/active-state cross-rule are all checked before any GPIO device
  is created.
- **Known limitation:** there is **no maximum pump run time**. If the
  sensor sticks at "dry" while the pump is running, the pump runs until
  the sensor recovers; a warning is logged when the minimum run time
  expires with the level still low. Size the top-up reservoir so that
  emptying it completely cannot overflow the aquarium, or add a max-run
  cutoff in `TopUp` if that guarantee is needed.

### Operational caveats

- Linux is not a real-time operating system: under load, transitions can
  occur slightly late, but minimum run and lockout periods are never
  shortened.
- Restarting the process resets both timing cycles (the lockout and the
  sweep cycle start over).
- Software cannot control pin voltage before the OS and this process
  configure the GPIO; use hardware pulls and drivers with safe default
  states if boot-time transients present a hazard.
- Before unattended operation, test on the actual hardware with the pump
  and motor disconnected or replaced by indicator loads, and verify every
  sensor state, timed transition, signal shutdown, and service restart.

## Testing

`test_topup.py` is a standard-library `unittest` suite — no hardware and
no extra dependencies required. Controller behaviour and safety paths
run against in-process fake devices; the full `run()` lifecycle runs
against [gpiozero's mock pin factory](https://gpiozero.readthedocs.io/en/stable/api_pins.html#mock-pins).
Timings are scaled to fractions of a second, transition timestamps are
recorded, and lower bounds are asserted for every timed phase.

```bash
python3 test_topup.py        # or: python3 -m unittest -v
```

Covered scenarios:

- **Behaviour:** start-up with the level full (pump stays off, lockout
  begins) and low (pump on immediately); the lockout holds the pump off
  despite low water and releases on expiry; the minimum run holds the
  pump on despite a restored level; the sweep starts with its off period
  and honours both phase durations; shutdown while running forces
  outputs off promptly.
- **Safety paths:** a stop event set before thread start never energises
  an output; an output failure — even when the fail-safe `off()` also
  raises — still sets the shared stop event and the crash flag; the
  minimum-run warning is emitted when the level stays low; a partial
  device-creation failure still switches off and closes the devices that
  were created and exits non-zero.
- **Config validation:** both shipped config files; missing keys;
  negative timings; fractional, boolean, out-of-range, and duplicate
  GPIOs; both directions of the pull-up/active-state cross-rule; bounce
  values; missing files and malformed JSON (with the path in the error).

## Running as a service

Create `/etc/systemd/system/topup.service`, replacing `<user>` with the
account that should run the service:

```ini
[Unit]
Description=Aquarium top-up and skimmer sweep controller

[Service]
ExecStart=/usr/bin/python3 /home/<user>/topup/topup.py
WorkingDirectory=/home/<user>
User=<user>
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

The `User=` account must be in the `gpio` group. `systemctl stop`
sends SIGTERM, which the program handles by
switching both outputs off before exiting; `Restart=on-failure` pairs
with the non-zero exit code the program uses when a control thread
crashes, a worker fails to stop, or the config is invalid, while a
deliberate `systemctl stop` stays stopped.

### lgpio needs a writable working directory

gpiozero drives the pins through the lgpio library, and lgpio creates a
notification FIFO (`.lgd-nfy*`) **in the process's current working
directory** when the first device is opened. If that directory is not
writable by the service user, the lgpio pin factory fails to load — and
so does the `RPi.GPIO` compatibility shim, which is lgpio-backed on
current Raspberry Pi OS. gpiozero then silently falls back to the
legacy sysfs backend, which no longer works on current kernels, and the
service crash-loops.

- **Symptom:** the service exits with `OSError: [Errno 22] Invalid
  argument` from `gpiozero/pins/native.py`, preceded by
  `PinFactoryFallback: Falling back from lgpio: [Errno 2] No such file
  or directory: '.lgd-nfy-…'` warnings and `xCreatePipe: Can't set
  permissions` messages. Running the script by hand works, because an
  interactive shell starts in the home directory.
- **Solution:** the `WorkingDirectory=` line in the unit above.
  Any directory writable by the `User=` account works; the FIFO is
  removed again on clean exit.
