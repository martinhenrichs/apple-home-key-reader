# Apple Home Key Reader — Multi-Lock Fork

A fork of [kormax/apple-home-key-reader](https://github.com/kormax/apple-home-key-reader)
that hosts **N virtual Home Key locks on a single PN532 reader**, so that
**multiple independent Apple Homes** (different people, different iCloud
accounts) can each provision their own Home Key and unlock at the same
physical reader.

All credit for the Home Key protocol implementation goes to
[@kormax](https://github.com/kormax) — see the
[upstream project](https://github.com/kormax/apple-home-key-reader) and
[kormax/apple-enhanced-contactless-polling](https://github.com/kormax/apple-enhanced-contactless-polling)
for the underlying research. This fork only adds an orchestration layer
(`lockhost.py`) plus two small patches on top of upstream commit `5c39b9c`.

## What this fork changes

| File | Change |
| --- | --- |
| `lockhost.py` | **New.** Multi-lock host: N virtual HAP locks, one shared NFC loop with ECP broadcast rotation and identity fallback, local HTTP pairing API, hot-add of new locks at runtime. |
| `lockhost.service` | **New.** Example systemd unit for running the lockhost. |
| `service.py` | Patched: a HAP unpair event no longer deletes enrolled issuers from `homekey.json` (see [Never unpair](#never-unpair-a-provisioned-lock)); added an `on_nfc_tag` hook for plain (non-Home-Key) NFC tags. |
| `README.md` | This document. For hardware setup, configuration reference, terminology and protocol background, keep the [upstream README](https://github.com/kormax/apple-home-key-reader#readme) at hand. |

The `main` branch of this fork tracks upstream unmodified; all changes live
on the `multi-lock` branch.

## The problem

Upstream runs **one** virtual lock. When you pair it with an Apple Home,
that Home writes its `reader_private_key` into the reader's repository
(`homekey.json`). Home Key passes are cryptographically bound to that key.
If a *second* Home pairs the same accessory, it **overwrites** the reader
key — instantly invalidating every pass provisioned by the first Home. One
accessory instance can therefore serve exactly **one** Apple Home.

That is fine for a household, but not for a shared door (office, workshop,
co-working space) where every user brings their *own* Apple Home and should
still get a real Home Key with Express Mode in their Wallet.

## The solution: lockhost

`lockhost.py` runs **N independent virtual locks in one process**:

* **One HAP accessory per lock.** Each lock is a full HomeKit lock accessory
  on its own TCP port with its own persist file — pair each one with a
  different Apple Home.
* **One repository per lock.** Each lock stores its own
  `reader_private_key`, reader identifiers and enrolled issuers/endpoints in
  `locks/<id>.homekey.json`. Homes can no longer clobber each other's keys.
* **HAP-only lock instances.** The lock objects never touch the PN532.
* **One shared NFC loop.** A single loop owns the PN532 exclusively and:
  1. **ECP broadcast rotation** — each polling cycle broadcasts the
     [ECP](https://github.com/kormax/apple-enhanced-contactless-polling)
     Home frame of the *next* active lock (round-robin). A device whose pass
     belongs to the broadcast identity wakes up in Express Mode.
  2. **Identity fallback** — when a device answers, authentication is first
     attempted against the lock whose identity was just broadcast, then
     against every other active lock until one matches. A tap therefore
     succeeds even when "the wrong" identity happened to be on air.
* **Hot-add.** New locks can be claimed or created at runtime through the
  HTTP API — no restart required.
* **Random default names.** New locks are named `Home Key Lock [100-999]`
  unless you pass an explicit name.

### Why the locks must stay online 24/7

iOS keeps a Home Key pass fully active only while the paired accessory is
reachable. If the accessory stays offline for an extended period, **iOS
"parks" the pass** — Express taps stop working until the accessory is back
online and the Home has reconciled. Run the lockhost as an always-on
service and never take individual locks down for long.

### Flow behaviour and measured latencies

* The **first tap after provisioning** a pass negotiates the **STANDARD**
  flow (the reader learns and persists the new endpoint). Every subsequent
  tap uses the **FAST** flow / Express Mode.
* Measured on a Raspberry Pi with a UART PN532 and **two** paired Homes:
  * **181–242 ms** when the broadcast identity matched the tapping device
    (direct hit),
  * **470–603 ms** when authentication had to fall back through the other
    lock identity first.

  The fallback cost grows with the number of active locks; behaviour with
  more than a handful of locks (10+) has not been measured, so keep the
  lock count reasonable if sub-second worst-case taps matter to you.

### One process per PN532 — no exceptions

The PN532 must be opened by **exactly one process**. A second opener (for
example the stock single-lock `main.py` running alongside the lockhost)
wedges the hardware in a state that usually requires a power cycle. Stop
and disable any other reader service before starting the lockhost.

## Pairing HTTP API

The lockhost exposes a small JSON API on `127.0.0.1:8080` (change with the
`LOCKHOST_API_PORT` environment variable). It is meant for a local kiosk or
companion UI that walks users through pairing. It binds to loopback only —
put an authenticating reverse proxy in front of it if you need remote
access (the responses contain HAP setup codes).

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/locks` | List all locks with their state. |
| `GET` | `/api/pairing/<id>` | State of one lock. |
| `POST` | `/api/pairing/start` | Claim a free (unpaired, unprovisioned) lock or create a new one live. Body: `{"name": "...", "finish": "..."}` (both optional). Returns the lock info **including its HAP setup code**. |
| `POST` | `/api/pairing/<id>/finish` | Set the Home Key art finish of a lock. Body: `{"finish": "black" \| "tan" \| "gold" \| "silver"}`. |

Lock info object:

```json
{
  "id": "1a2b",
  "name": "Home Key Lock 421",
  "port": 51930,
  "code": "123-45-678",
  "hap_paired": true,
  "provisioned": true,
  "finish": "black"
}
```

* `hap_paired` — a Home has completed HAP pairing with this lock.
* `provisioned` — that Home has pushed its reader key, i.e. Home Key taps
  for this lock will authenticate.

Typical pairing flow: `POST /api/pairing/start` → show the returned setup
code to the user → the user adds the accessory in the Home app → poll
`GET /api/pairing/<id>` until `hap_paired` and `provisioned` are `true` →
optionally `POST /api/pairing/<id>/finish` to pick the pass artwork.

## Tap hook

After every successful read, `lockhost.py` calls `on_tap(lock, identifier,
kind)` — `kind` is `"homekey"` (identifier = endpoint id) or `"nfc"`
(identifier = plain tag UID; `lock` is `None` in that case). The default
implementation only logs. Edit `on_tap` (or pass your own `tap_callback` to
`MultiLockNFC`) to trigger whatever your application needs: HTTP call, MQTT
publish, GPIO pulse, …

## Installation

Hardware requirements and PN532 wiring are identical to upstream — see the
[upstream README](https://github.com/kormax/apple-home-key-reader#requirements).

```bash
git clone https://github.com/martinhenrichs/apple-home-key-reader.git
cd apple-home-key-reader
git checkout multi-lock

python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

Edit `configuration.json`: the `nfc` section (your PN532 port) and the
`homekey` defaults (`express`, `finish`, `flow`) are used by the lockhost;
the single-lock `hap` section is ignored by `lockhost.py`.

Create the first lock and run:

```bash
./venv/bin/python lockhost.py add            # random name, or: add "Front Door"
./venv/bin/python lockhost.py
```

State lives in `locks/`:

* `locks/locks.json` — the registry (id, name, port, finish per lock),
* `locks/<id>.hap.state` — HAP pairing state (**contains keys**),
* `locks/<id>.homekey.json` — reader private key + enrolled
  issuers/endpoints (**contains keys**).

### systemd

Adapt the paths and user in `lockhost.service`, then:

```bash
sudo cp lockhost.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lockhost.service
```

Set the `LOCKHOST_DIR` environment variable if `configuration.json` and the
`locks/` state directory should live somewhere other than the checkout.

## Operational rules

### Never unpair a provisioned lock

Removing the accessory from the Home app **revokes the user's Wallet pass**
— the Home deletes the pass and the stored reader key becomes useless.
Treat pairing as one-way: once a lock is provisioned, leave it alone. The
`service.py` patch in this fork supports that stance: upstream deletes
issuers from the repository as soon as their HAP pairing disappears (which
also destroys the enrollment history on an accidental unpair); this fork
keeps enrolled issuers in the repository and only logs the event. If you
want upstream's strict revocation semantics (removed pairing = credential
rejected immediately), revert that hunk.

### Back up `locks/`

`locks/` contains every reader private key, HAP pairing and enrolled
credential. Losing it means every user has to re-pair and re-provision.
Back it up (encrypted — these are secrets) and **never commit it to git**.

### Keep it running

Extended downtime parks your users' passes — see
[above](#why-the-locks-must-stay-online-247).

## Credits

* [kormax/apple-home-key-reader](https://github.com/kormax/apple-home-key-reader) —
  the entire Home Key protocol implementation this fork builds on.
* [kormax/apple-enhanced-contactless-polling](https://github.com/kormax/apple-enhanced-contactless-polling) —
  the ECP research that makes Express Mode and the broadcast rotation
  possible.
* Everyone credited in the
  [upstream README](https://github.com/kormax/apple-home-key-reader#credits).

## License

Same as upstream — see [LICENSE](./LICENSE).
