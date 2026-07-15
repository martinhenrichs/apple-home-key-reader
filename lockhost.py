"""Lockhost: N virtual Home Key locks sharing ONE PN532 reader.

Every user pairs THEIR own virtual lock with THEIR own Apple Home (each
Home dictates its own reader_private_key, therefore each lock needs its
own repository). A single NFC polling loop rotates the ECP broadcast
across all active reader identities and authenticates taps against the
matching lock. The lock instances are HAP-only (no PN532 access) and
must stay online permanently, otherwise iOS parks the Wallet pass.

Registry: $LOCKHOST_DIR/locks/locks.json
  [{"id": "a1", "name": "Home Key Lock 123", "port": 51930}, ...]
Per lock:  locks/<id>.hap.state + locks/<id>.homekey.json

Usage:   lockhost.py            -> start all locks + the NFC loop
         lockhost.py add <Name> -> register a new lock in the registry

IMPORTANT: Only ONE process may open the PN532. Stop any other reader
service (e.g. the stock single-lock main.py) before running lockhost.
"""
import json
import logging
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.environ.get(
    "LOCKHOST_DIR", os.path.dirname(os.path.abspath(__file__))
)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pyhap.accessory_driver import AccessoryDriver

from accessory import Lock
from entity import HardwareFinishColor
from homekey import read_homekey, ProtocolError
from main import configure_logging, configure_nfc_device, load_configuration
from repository import Repository
from service import Service
from util.bfclf import RemoteTarget, activate, ISODEPTag
from util.digital_key import DigitalKeyFlow, DigitalKeyTransactionType
from util.ecp import ECP
from util.iso7816 import ISO7816Tag

log = logging.getLogger()

LOCKS_DIR = os.path.join(BASE_DIR, "locks")
REGISTRY = os.path.join(LOCKS_DIR, "locks.json")
BASE_PORT = 51930
EMPTY_KEY = bytes.fromhex("00" * 32)
API_PORT = int(os.environ.get("LOCKHOST_API_PORT", "8080"))
FINISHES = ("black", "tan", "gold", "silver")


def load_registry():
    try:
        return json.load(open(REGISTRY))
    except FileNotFoundError:
        return []


def save_registry(entries):
    os.makedirs(LOCKS_DIR, exist_ok=True)
    json.dump(entries, open(REGISTRY, "w"), indent=2)


def random_lock_name(taken):
    """'Home Key Lock [100-999]', collision-free against the registry."""
    while True:
        name = "Home Key Lock %d" % (int.from_bytes(os.urandom(2), "big") % 900 + 100)
        if name not in taken:
            return name


def add_lock(name=None):
    entries = load_registry()
    used_ports = {e["port"] for e in entries}
    port = BASE_PORT
    while port in used_ports:
        port += 1
    lock_id = "%04x" % (int.from_bytes(os.urandom(2), "big"))
    name = name or random_lock_name({e["name"] for e in entries})
    entries.append({"id": lock_id, "name": name, "port": port})
    save_registry(entries)
    print("Lock created: id=%s name=%r port=%s" % (lock_id, name, port))
    print("Restart lockhost to bring it online.")


class HostedLock:
    """One virtual lock: its own repository + a HAP-only accessory."""

    def __init__(self, entry, homekey_cfg):
        self.entry = entry
        self.repository = Repository(
            os.path.join(LOCKS_DIR, "%s.homekey.json" % entry["id"])
        )
        # Each lock gets its own HAP service: it receives the reader key
        # + device credentials of its paired Home and writes them into
        # THIS lock's repository.
        self.service = Service(
            None,
            repository=self.repository,
            express=homekey_cfg.get("express", True),
            finish=entry.get("finish") or homekey_cfg.get("finish", "black"),
            flow=homekey_cfg.get("flow", "fast"),
        )
        self.driver = AccessoryDriver(
            port=entry["port"],
            persist_file=os.path.join(LOCKS_DIR, "%s.hap.state" % entry["id"]),
        )
        accessory = Lock(
            self.driver, entry["name"], service=self.service, lock_state_at_startup=1
        )
        self.driver.add_accessory(accessory=accessory)

    def active(self):
        key = self.repository.get_reader_private_key()
        return key not in (None, b"") and key != EMPTY_KEY

    def set_finish(self, finish):
        self.service.hardware_finish_color = HardwareFinishColor[finish.upper()]
        self.entry["finish"] = finish

    def info(self):
        return {
            "id": self.entry["id"],
            "name": self.entry["name"],
            "port": self.entry["port"],
            "code": self.driver.state.pincode.decode(),
            "hap_paired": self.driver.state.paired,
            "provisioned": self.active(),
            "finish": self.entry.get("finish"),
        }

    def start(self):
        threading.Thread(target=self.driver.start, daemon=True).start()
        log.info(
            "Lock %r (id=%s) on port %s, setup code %s",
            self.entry["name"], self.entry["id"], self.entry["port"],
            self.driver.state.pincode.decode(),
        )


class MultiLockNFC:
    """One NFC loop for all locks: ECP rotation + identity fallback."""

    def __init__(self, clf, locks, express=True, flow=None, tap_callback=None):
        self.clf = clf
        self.locks = locks
        self.express = express
        self.flow = flow
        self.tap_callback = tap_callback
        self._rot = 0
        self._run_flag = True

    def _attempt(self, tag, lock):
        repo = lock.repository
        result_flow, new_issuers_state, endpoint = read_homekey(
            tag,
            issuers=repo.get_all_issuers(),
            preferred_versions=[b"\x02\x00"],
            flow=self.flow,
            transaction_code=DigitalKeyTransactionType.UNLOCK,
            reader_identifier=repo.get_reader_group_identifier()
            + repo.get_reader_identifier(),
            reader_private_key=repo.get_reader_private_key(),
            key_size=16,
        )
        if new_issuers_state is not None and len(new_issuers_state):
            repo.upsert_issuers(new_issuers_state)
        return result_flow, endpoint

    def _read_once(self):
        active = [l for l in self.locks if l.active()]
        if not active:
            time.sleep(1.0)
            return

        self._rot = (self._rot + 1) % len(active)
        primary = active[self._rot]

        remote_target = self.clf.sense(
            RemoteTarget("106A"),
            broadcast=ECP.home(
                identifier=primary.repository.get_reader_group_identifier(),
                flag_2=self.express,
            ).pack(),
        )
        if remote_target is None:
            return
        target = activate(self.clf, remote_target)
        if target is None:
            return

        if not isinstance(target, ISODEPTag):
            log.info("Non-ISODEP tag UID: %s", target.identifier.hex().upper())
            if self.tap_callback:
                self.tap_callback(None, target.identifier.hex(), "nfc")
            while self.clf.sense(RemoteTarget("106A")) is not None:
                time.sleep(0.5)
            return

        log.info("Got NFC tag %s (primary lock=%s)", target, primary.entry["id"])
        tag = ISO7816Tag(target)
        start = time.monotonic()

        # Try the identity we just broadcast first, then all the others.
        order = [primary] + [l for l in active if l is not primary]
        for lock in order:
            try:
                result_flow, endpoint = self._attempt(tag, lock)
            except ProtocolError as e:
                log.info(
                    "Lock %s: identity does not match (%s) - trying next...",
                    lock.entry["id"], e,
                )
                continue
            except Exception as e:
                # Torn transaction (device removed too early etc.):
                # do NOT restart the loop, resume polling immediately.
                log.info("Transaction aborted (%s) - resuming polling", e)
                return
            log.info(
                "Authenticated via %r on lock %s (%s) in %d ms",
                result_flow, lock.entry["id"], lock.entry["name"],
                (time.monotonic() - start) * 1000,
            )
            if endpoint is not None:
                lock.service.on_endpoint_authenticated(endpoint)
                if self.tap_callback:
                    self.tap_callback(lock, endpoint.id.hex(), "homekey")
            break
        else:
            log.info("No lock identity matched this device.")

        try:
            while target.is_present:
                time.sleep(0.5)
        except Exception:
            pass
        log.info("Device left the field. Continuing in 2 seconds...")
        time.sleep(2)
        log.info("Waiting for next device...")

    def run(self):
        self.clf.device = None
        self.clf.open(self.clf.path)
        if self.clf.device is None:
            raise Exception("PN532 not reachable: %s" % self.clf.path)
        log.info("NFC loop: %d locks registered", len(self.locks))
        while self._run_flag:
            try:
                self._read_once()
            except ProtocolError:
                pass
            except Exception:
                log.exception("NFC loop: error, retrying in 5s")
                time.sleep(5)
                try:
                    self.clf.device = None
                    self.clf.open(self.clf.path)
                except Exception:
                    log.exception("PN532 re-open failed")


class Lockhost:
    """Manage running locks: hand out a free one or create one live."""

    def __init__(self, locks, homekey_cfg):
        self.locks = locks
        self.homekey_cfg = homekey_cfg
        self.mutex = threading.Lock()

    def save(self):
        save_registry([l.entry for l in self.locks])

    def get(self, lock_id):
        for hl in self.locks:
            if hl.entry["id"] == lock_id:
                return hl
        return None

    def claim_or_create(self, name, finish):
        with self.mutex:
            for hl in self.locks:
                if not hl.active() and not hl.driver.state.paired:
                    if name:
                        hl.entry["name"] = name
                    if finish:
                        hl.set_finish(finish)
                    self.save()
                    log.info(
                        "Pairing: assigned free lock %s to %r",
                        hl.entry["id"], hl.entry["name"],
                    )
                    return hl
            used_ports = {l.entry["port"] for l in self.locks}
            used_ids = {l.entry["id"] for l in self.locks}
            port = BASE_PORT
            while port in used_ports:
                port += 1
            lock_id = "%04x" % int.from_bytes(os.urandom(2), "big")
            while lock_id in used_ids:
                lock_id = "%04x" % int.from_bytes(os.urandom(2), "big")
            entry = {
                "id": lock_id,
                "name": name or random_lock_name({l.entry["name"] for l in self.locks}),
                "port": port,
            }
            if finish:
                entry["finish"] = finish
            hl = HostedLock(entry, self.homekey_cfg)
            self.locks.append(hl)
            self.save()
            hl.start()
            log.info(
                "Pairing: created new lock %s (%r) live",
                entry["id"], entry["name"],
            )
            return hl


class ApiHandler(BaseHTTPRequestHandler):
    """Local pairing API for a kiosk/companion frontend (127.0.0.1 only)."""

    host = None

    def log_message(self, fmt, *args):
        log.debug("API: " + fmt, *args)

    def _send(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send({})

    def do_GET(self):
        if self.path == "/api/locks":
            return self._send([l.info() for l in self.host.locks])
        m = re.match(r"^/api/pairing/([0-9a-f]+)$", self.path)
        if m:
            hl = self.host.get(m.group(1))
            if hl is None:
                return self._send({"error": "unknown lock"}, 404)
            return self._send(hl.info())
        self._send({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            data = {}
        finish = data.get("finish")
        if finish is not None and finish not in FINISHES:
            return self._send({"error": "invalid finish"}, 400)
        if self.path == "/api/pairing/start":
            hl = self.host.claim_or_create(data.get("name"), finish)
            return self._send(hl.info())
        m = re.match(r"^/api/pairing/([0-9a-f]+)/finish$", self.path)
        if m:
            hl = self.host.get(m.group(1))
            if hl is None:
                return self._send({"error": "unknown lock"}, 404)
            if finish is None:
                return self._send({"error": "invalid finish"}, 400)
            hl.set_finish(finish)
            self.host.save()
            return self._send(hl.info())
        self._send({"error": "not found"}, 404)


def start_api(host):
    server = ThreadingHTTPServer(("127.0.0.1", API_PORT), ApiHandler)
    ApiHandler.host = host
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Pairing API on 127.0.0.1:%s", API_PORT)


def on_tap(lock, identifier, kind):
    """Hook point: called after every successful authentication.

    lock:       the HostedLock that matched (None for plain NFC tags)
    identifier: endpoint id (Home Key) or tag UID (plain NFC), hex string
    kind:       "homekey" or "nfc"

    Replace the body with whatever your application needs (HTTP call,
    MQTT publish, GPIO pulse, ...). The default just logs the event.
    """
    log.info(
        "Tap: kind=%s identifier=%s lock=%s",
        kind, identifier, lock.entry["id"] if lock else "-",
    )


def main():
    os.chdir(BASE_DIR)
    config = load_configuration(os.path.join(BASE_DIR, "configuration.json"))
    configure_logging(config["logging"])

    entries = load_registry()
    if not entries:
        log.warning("No locks in %s - create one with 'lockhost.py add <Name>'", REGISTRY)

    locks = [HostedLock(e, config["homekey"]) for e in entries]
    for lock in locks:
        lock.start()

    start_api(Lockhost(locks, config["homekey"]))

    nfc = MultiLockNFC(
        configure_nfc_device(config["nfc"]),
        locks,
        express=config["homekey"].get("express", True),
        flow=DigitalKeyFlow[config["homekey"].get("flow", "fast").upper()],
        tap_callback=on_tap,
    )
    nfc.run()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "add":
        add_lock(" ".join(sys.argv[2:]) or None)
    else:
        main()
