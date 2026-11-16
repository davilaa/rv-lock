# RV Lock (Tuya BLE)

A Home Assistant custom integration for a **Kohree / Tuya Bluetooth RV door lock**, written to
talk to the lock the way the Smart Life app does: local Bluetooth only, no cloud.

Built for one specific lock (Kohree keypad + fingerprint RV lock, Tuya product `f9amgmrh`). The
protocol is the standard Tuya BLE v4 lock path, so similar Tuya locks may work, but only this one
has been tested.

The repository also holds an optional ESPHome component ([`esphome/`](esphome/)) for a classic
ESP32 Bluetooth proxy. It manages the lock's link on the proxy. On the author's setup it is what
brought a command sent to a sleeping lock down to about two seconds (see Speed and battery).

> Not affiliated with or endorsed by Kohree or Tuya. Use at your own risk: this controls a door lock.

## Features

- `lock` entity, plus separate **Lock** and **Unlock** buttons (handy for HomeKit/Siri).
- Two connection modes, switched from the lock's device page:
  - **On demand** (default): the Bluetooth link is dropped after a short idle time and re-made by
    the next command, so the lock's radio sleeps almost all day.
  - **Held**: the link stays up and is kept warm, every command takes a fraction of a second, and
    the lock's battery drains faster.
- Works through an **ESPHome Bluetooth proxy** (preferred when one hears the lock) or the Home
  Assistant host adapter (fallback). On the host adapter it forces the ATT MTU up so commands are
  full speed there too.
- Battery, signal, last-command-time, "last unlocked by", connection and fingerprint-enrolment sensors.
- A **Reachable** binary sensor for alerting: on while the lock is connected or a scanner that can
  connect to it (an active proxy or the host adapter) heard it advertising in the last three minutes. Use it instead of **Connected**, which is off most of
  the time in on-demand mode.
- Credential management as services: permanent PINs, temporary PINs (with expiry, use count,
  weekday and daily-window limits), and fingerprint enroll/delete.
- A `credentials` sensor listing the PINs and fingerprints this integration created.

## Entities

| Entity | What it is |
| --- | --- |
| Lock | The lock itself |
| Buttons: Lock, Unlock | One-shot commands |
| Button: Reconnect | Connect now (also ends a release) |
| Button: Release for app (15 min) | Drop the link and stay away so the phone app can connect |
| Switch: Idle disconnect (battery saver) | On = on-demand mode, off = held link |
| Number: Idle timeout | Seconds without a command before the link is dropped (15 to 1800) |
| Binary sensors: Connected, Reachable | Link state, and the health signal described above |
| Sensors | Battery, Signal, Last command time, Last reconnect time, Connected since, Last connected time, Last unlocked by, Fingerprint enrolment, Credentials |

## Speed and battery

Measured on the author's lock, about two feet from a classic ESP32 proxy running the component
in [`esphome/`](esphome/):

| Case | Time |
| --- | --- |
| Command on an open link (30 ms hold interval) | 0.2 to 0.5 s |
| First command after the link was dropped | about 1.8 s typical; 1.5 to 3.5 s in most runs, up to about 5 s when the lock's next advertisement is slow to be heard |

The lock's battery gauge fell about 3 % per day while a link was held all day, against about
0.2 % per day with no link. That is why on-demand mode is the default.

Through the Home Assistant host adapter a first command after the link was dropped takes
noticeably longer. A stock ESPHome proxy, without the component, was not dependable with this lock
(see below).

## Requirements

- Home Assistant with the **Bluetooth** integration set up: one or more ESPHome Bluetooth proxies
  with `bluetooth_proxy: active: true` in range of the lock, or a host adapter. Developed and tested on Home Assistant 2026.11.
- The lock's Tuya **local key**, **device id** and **uuid**. These come from the Tuya / Smart Life
  account the lock is paired to. Keep them private: they are entered once at setup and stored in
  the Home Assistant config entry, never in this repository.

## Install

### Home Assistant store / HACS (custom repository)

1. Open the store, three-dot menu > **Custom repositories**.
2. Add `https://github.com/davilaa/rv-lock`, category **Integration**.
3. Install **RV Lock (Tuya BLE)**, then restart Home Assistant.

### Manual

Copy `custom_components/rv_lock` into your Home Assistant `config/custom_components/` folder and
restart.

## Setup

Settings > Devices & Services > **Add Integration** > **RV Lock**. Enter the lock's Bluetooth
address (`AA:BB:CC:DD:EE:FF`), local key, uuid, device id, the unlock passcode (the 8-digit code
sent with a Bluetooth unlock; leave the default unless yours differs) and, optionally, the Bluetooth
MAC address of your ESPHome proxy (not its Wi-Fi MAC). That last field does not choose the path:
any connectable proxy that hears the lock is used, strongest signal first. It only lets the
integration ask that proxy to drop a stuck link to the lock, and it can be left empty.

If the `ha_kohree` integration is already set up for the same lock, its keys are imported instead
and no form is shown; the proxy address is then left empty. Disable or remove `ha_kohree`
afterwards: the lock accepts one Bluetooth connection at a time.

## Services

| Service | What it does |
| --- | --- |
| `rv_lock.create_pin` | Add a permanent keypad PIN (name, 6-10 digit code, optional admin). |
| `rv_lock.create_temporary_pin` | Add a time-limited PIN (expiry required; optional start, use count, weekdays, daily window). |
| `rv_lock.delete_pin` | Remove a PIN by name. |
| `rv_lock.enroll_fingerprint` | Start fingerprint enrollment; press the finger on the reader several times. |
| `rv_lock.delete_fingerprint` | Remove a fingerprint by name. |
| `rv_lock.forget_credential` | Drop a stored PIN or fingerprint record without talking to the lock (every record when no name is given). |
| `rv_lock.list_credentials` | Return the PINs and fingerprints this integration stored. |

## Optional: the ESP32 proxy component

With a stock ESPHome Bluetooth proxy this lock was not dependable on the author's setup: ESPHome
sets the link up at 7.5 ms, and the lock often did not answer the connect request or dropped the
link a few seconds later. The component in [`esphome/`](esphome/) is what made the proxy path
dependable and fast on a classic ESP32: it creates the link at 30 ms, gives Bluetooth a larger
share of the radio while a connect is waiting for the lock's advertisement, keeps the link at
30 ms through the login, and reports per-connect timings as sensors. See
[`esphome/README.md`](esphome/README.md).

## Known issues and notes

- **One BLE connection at a time.** While Home Assistant holds the lock, the phone app cannot
  connect. Use **Release for app** to hand the link over and **Reconnect** to take it back.
- **Do not hammer the lock.** After a burst of back-to-back connects the lock's firmware can stop
  answering logins for a long time. The integration spaces its attempts and backs off; do not
  script rapid reconnect loops around it.
- **About one connect in ten** on the author's setup has a connect request the lock does not answer; the Bluetooth
  stack retries by itself and that connect takes a few seconds longer.
- **The battery gauge is coarse**: whole percent, refreshed about twice a day.
- **Codes are 6 to 10 digits.** PINs made here are stored by the integration and by the lock; the
  phone app's own codes are not listed here.
- **Fingerprint data cannot be read out.** You can enroll, delete and watch progress only.
- The lock's own firmware is signed by Tuya and is not modified by this integration.
- The cached proxy connect relies on Home Assistant's `bleak-esphome` client. It checks for the
  option at run time and falls back to the normal connect if a Home Assistant update removes it.

Dashboards, automations and input helpers are Home Assistant configuration, not part of the
integration; set them up separately.

## Tools

[`tools/`](tools/) holds the diagnostic and bench scripts used while developing this. None of
them is needed to use the integration.

## License

MIT. See [LICENSE](LICENSE).
