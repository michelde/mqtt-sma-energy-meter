#!/usr/bin/env python3
"""
MQTT → SMA EMETER Speedwire Bridge
====================================
Abonniert MQTT-Topics und sendet die empfangenen Leistungs- und Energiewerte
als SMA Speedwire EMETER UDP-Multicast-Pakete, damit der SMA Home Manager 2.0
die Quellen als virtuelle Erzeuger erkennt.

Mehrere virtuelle EMETER (unterschiedliche Seriennummern) werden gleichzeitig
unterstützt. Konfiguration erfolgt über eine YAML-Datei (Standard: /app/config.yaml).

SMA EMETER Speedwire Protokoll (v1.0):
  Multicast-Adresse : 239.12.255.254
  UDP-Port          : 9522
"""

import argparse
import json
import logging
import os
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
import yaml
from jsonpath_ng import parse as jparse

# ---------------------------------------------------------------------------
# SMA EMETER Protokoll-Konstanten
# ---------------------------------------------------------------------------

SMA_EMETER_MCAST_ADDR = "239.12.255.254"
SMA_EMETER_UDP_PORT   = 9522

OBIS_P_CONSUME_W  = 0x00010400
OBIS_E_CONSUME_WH = 0x00010800
OBIS_P_SUPPLY_W   = 0x00020400
OBIS_E_SUPPLY_WH  = 0x00020800

SUSYID = 0x010E   # SMA Energy Meter (SusyID 270)

OBIS_DUMMY_SEQUENCE = [
    ('M32', 0x00030400), ('C64', 0x00030800),
    ('M32', 0x00040400), ('C64', 0x00040800),
    ('M32', 0x00090400), ('C64', 0x00090800),
    ('M32', 0x000A0400), ('C64', 0x000A0800),
    ('M32', 0x000D0400),
    ('M32', 0x00150400), ('C64', 0x00150800),
    ('M32', 0x00160400), ('C64', 0x00160800),
    ('M32', 0x00170400), ('C64', 0x00170800),
    ('M32', 0x00180400), ('C64', 0x00180800),
    ('M32', 0x001D0400), ('C64', 0x001D0800),
    ('M32', 0x001E0400), ('C64', 0x001E0800),
    ('M32', 0x001F0400),
    ('M32', 0x00200400),
    ('M32', 0x00210400),
    ('M32', 0x00290400), ('C64', 0x00290800),
    ('M32', 0x002A0400), ('C64', 0x002A0800),
    ('M32', 0x002B0400), ('C64', 0x002B0800),
    ('M32', 0x002C0400), ('C64', 0x002C0800),
    ('M32', 0x00310400), ('C64', 0x00310800),
    ('M32', 0x00320400), ('C64', 0x00320800),
    ('M32', 0x00330400),
    ('M32', 0x00340400),
    ('M32', 0x00350400),
    ('M32', 0x003D0400), ('C64', 0x003D0800),
    ('M32', 0x003E0400), ('C64', 0x003E0800),
    ('M32', 0x003F0400), ('C64', 0x003F0800),
    ('M32', 0x00400400), ('C64', 0x00400800),
    ('M32', 0x00450400), ('C64', 0x00450800),
    ('M32', 0x00460400), ('C64', 0x00460800),
    ('M32', 0x00470400),
    ('M32', 0x00480400),
    ('M32', 0x00490400),
]

# ---------------------------------------------------------------------------
# EMETER-Paket bauen (übernommen aus hoymiles-sma-emeter)
# ---------------------------------------------------------------------------

def build_emeter_packet(
    serial: int,
    power_w: float,
    energy_wh: float,
    ticker: int,
) -> bytes:
    buf = bytearray(1000)

    def w16(p: int, v: int) -> int:
        buf[p] = (v >> 8) & 0xFF; buf[p+1] = v & 0xFF
        return p + 2

    def w32(p: int, v: int) -> int:
        return w16(w16(p, (v >> 16) & 0xFFFF), v & 0xFFFF)

    def w64(p: int, v: int) -> int:
        return w32(w32(p, (v >> 32) & 0xFFFFFFFF), v & 0xFFFFFFFF)

    buf[0:4] = b"SMA\x00"
    pos = w16(4,  0x0004)
    pos = w16(pos, 0x02A0)
    pos = w32(pos, 0x00000001)
    data_size_pos = pos
    pos = w16(pos, 0x0000)
    pos = w16(pos, 0x0010)
    pos = w16(pos, 0x6069)
    pos = w16(pos, SUSYID)
    pos = w32(pos, serial)
    pos = w32(pos, ticker)

    payload_len = 12

    pos = w32(pos, OBIS_P_CONSUME_W);  pos = w32(pos, 0); payload_len += 8
    pos = w32(pos, OBIS_E_CONSUME_WH); pos = w64(pos, 0); payload_len += 12
    pos = w32(pos, OBIS_P_SUPPLY_W);   pos = w32(pos, max(0, int(round(power_w * 10)))); payload_len += 8
    pos = w32(pos, OBIS_E_SUPPLY_WH);  pos = w64(pos, max(0, int(round(energy_wh * 3600)))); payload_len += 12

    for typ, obis in OBIS_DUMMY_SEQUENCE:
        pos = w32(pos, obis)
        if typ == 'M32':
            pos = w32(pos, 0); payload_len += 8
        else:
            pos = w64(pos, 0); payload_len += 12

    pos = w32(pos, 0x90000000); pos = w32(pos, 0x01020452); payload_len += 8

    w16(data_size_pos, payload_len)
    pos = w32(pos, 0x00000000)

    total_len = 28 + payload_len - 12 + 4
    return bytes(buf[:total_len])


# ---------------------------------------------------------------------------
# UDP-Sender (übernommen aus hoymiles-sma-emeter)
# ---------------------------------------------------------------------------

class EMETERSender:
    def __init__(
        self,
        serial: int,
        mcast_addr: str = SMA_EMETER_MCAST_ADDR,
        port: int       = SMA_EMETER_UDP_PORT,
        interface: str  = "",
    ):
        self.serial    = serial
        self.mcast_addr = mcast_addr
        self.port      = port
        self.interface = interface
        self._sock     = self._create_socket()

    def _create_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 32)
        if self.interface:
            if re.match(r'^\d+\.\d+\.\d+\.\d+$', self.interface):
                sock.setsockopt(
                    socket.IPPROTO_IP,
                    socket.IP_MULTICAST_IF,
                    socket.inet_aton(self.interface),
                )
            else:
                sock.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_BINDTODEVICE,
                    self.interface.encode(),
                )
        return sock

    def _ticker(self) -> int:
        return int(time.time() * 1000) & 0xFFFFFFFF

    def send(self, power_w: float, energy_wh: float) -> None:
        pkt = build_emeter_packet(
            serial=self.serial,
            power_w=power_w,
            energy_wh=energy_wh,
            ticker=self._ticker(),
        )
        self._sock.sendto(pkt, (self.mcast_addr, self.port))

    def close(self) -> None:
        self._sock.close()


# ---------------------------------------------------------------------------
# Meter-Zustand
# ---------------------------------------------------------------------------

@dataclass
class EMeterState:
    serial: int
    topic_power: str
    topic_power_path: Optional[Any]         # kompilierter jsonpath-ng Ausdruck oder None
    topic_energy_total: Optional[str]       # None = keine Energie-Erfassung
    topic_energy_total_path: Optional[Any]  # kompilierter jsonpath-ng Ausdruck oder None
    topic_energy_total_unit: str            # "Wh" oder "kWh"
    power_w: float = 0.0
    energy_wh: float = 0.0
    last_update: float = field(default_factory=time.time)
    timed_out: bool = False
    is_static: bool = False
    sender: Optional[EMETERSender] = None


# ---------------------------------------------------------------------------
# Konfiguration laden und validieren
# ---------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    if not path.exists():
        print(f"ERROR: Config-Datei nicht gefunden: {path}", file=sys.stderr)
        sys.exit(1)

    with open(path) as f:
        cfg = yaml.safe_load(f) or {}

    # MQTT-Defaults
    mqtt_cfg = cfg.setdefault("mqtt", {})
    mqtt_cfg.setdefault("port", 1883)
    mqtt_cfg.setdefault("username", "")
    mqtt_cfg.setdefault("password", "")
    mqtt_cfg.setdefault("timeout", 300)

    # ENV-Overrides für alle MQTT-Parameter (Priorität: ENV > config.yaml)
    if os.environ.get("MQTT_BROKER"):
        mqtt_cfg["broker"] = os.environ["MQTT_BROKER"]
    if os.environ.get("MQTT_PORT"):
        mqtt_cfg["port"] = int(os.environ["MQTT_PORT"])
    if os.environ.get("MQTT_USERNAME") is not None:
        mqtt_cfg["username"] = os.environ["MQTT_USERNAME"]
    if os.environ.get("MQTT_PASSWORD") is not None:
        mqtt_cfg["password"] = os.environ["MQTT_PASSWORD"]
    if os.environ.get("MQTT_TIMEOUT"):
        mqtt_cfg["timeout"] = int(os.environ["MQTT_TIMEOUT"])

    if not mqtt_cfg.get("broker"):
        print("ERROR: mqtt.broker ist nicht konfiguriert (config.yaml oder MQTT_BROKER).", file=sys.stderr)
        sys.exit(1)

    # Globale Defaults
    cfg.setdefault("interface", "")
    cfg.setdefault("log_level", "INFO")

    valid_log_levels = {"DEBUG", "INFO", "WARNING", "ERROR"}
    if cfg["log_level"].upper() not in valid_log_levels:
        print(f"ERROR: log_level muss einer von {valid_log_levels} sein.", file=sys.stderr)
        sys.exit(1)
    cfg["log_level"] = cfg["log_level"].upper()

    # Meter validieren
    emeters = cfg.get("emeters")
    if not emeters:
        print("ERROR: Keine emeters konfiguriert.", file=sys.stderr)
        sys.exit(1)

    seen_serials = set()
    for i, em in enumerate(emeters):
        if not isinstance(em.get("serial"), int) or em["serial"] <= 0:
            print(f"ERROR: emeters[{i}].serial fehlt oder ist ungültig.", file=sys.stderr)
            sys.exit(1)
        if em["serial"] in seen_serials:
            print(f"ERROR: Seriennummer {em['serial']} wird mehrfach verwendet.", file=sys.stderr)
            sys.exit(1)
        seen_serials.add(em["serial"])

        is_static = "static_power_w" in em
        if not is_static and not em.get("topic_power"):
            print(f"ERROR: emeters[{i}].topic_power fehlt (oder static_power_w setzen).", file=sys.stderr)
            sys.exit(1)

        unit = em.get("topic_energy_total_unit", "Wh")
        if unit not in ("Wh", "kWh"):
            print(f"ERROR: emeters[{i}].topic_energy_total_unit muss 'Wh' oder 'kWh' sein.", file=sys.stderr)
            sys.exit(1)
        em["topic_energy_total_unit"] = unit

    return cfg


# ---------------------------------------------------------------------------
# Topic-Map aufbauen
# ---------------------------------------------------------------------------

def build_topic_map(
    states: list[EMeterState],
    log: logging.Logger,
) -> dict[str, list[tuple[EMeterState, str]]]:
    topic_map: dict[str, list[tuple[EMeterState, str]]] = {}

    for state in states:
        if state.topic_power:
            topic_map.setdefault(state.topic_power, []).append((state, "power"))
        if state.topic_energy_total:
            topic_map.setdefault(state.topic_energy_total, []).append((state, "energy"))

    # Warn bei geteilten Topics
    for topic, entries in topic_map.items():
        serials = [s.serial for s, _ in entries]
        if len(serials) > 1:
            log.debug("Topic '%s' wird von mehreren Metern genutzt (serials: %s)", topic, serials)

    return topic_map


# ---------------------------------------------------------------------------
# Payload-Wert extrahieren
# ---------------------------------------------------------------------------

def extract_value(payload_str: str, path_expr: Any, log: logging.Logger) -> Optional[float]:
    try:
        data = json.loads(payload_str)
    except json.JSONDecodeError as e:
        log.warning("JSON-Parse-Fehler in Payload '%s': %s", payload_str[:80], e)
        return None

    matches = path_expr.find(data)
    if not matches:
        log.warning("JSON-Path liefert kein Ergebnis für Payload '%s'", payload_str[:80])
        return None

    try:
        return float(matches[0].value)
    except (TypeError, ValueError) as e:
        log.warning("Wert '%s' ist keine Zahl: %s", matches[0].value, e)
        return None


def _update_state(
    state: EMeterState,
    field_name: str,
    payload: str,
    log: logging.Logger,
) -> None:
    if field_name == "power":
        if state.topic_power_path is not None:
            value = extract_value(payload, state.topic_power_path, log)
        else:
            try:
                value = float(payload)
            except ValueError:
                log.warning("[serial=%d] Power-Payload nicht konvertierbar: '%s'", state.serial, payload[:80])
                return
        if value is not None:
            state.power_w = value

    elif field_name == "energy":
        if state.topic_energy_total_path is not None:
            value = extract_value(payload, state.topic_energy_total_path, log)
        else:
            try:
                value = float(payload)
            except ValueError:
                log.warning("[serial=%d] Energy-Payload nicht konvertierbar: '%s'", state.serial, payload[:80])
                return
        if value is not None:
            if state.topic_energy_total_unit == "kWh":
                value *= 1000.0
            state.energy_wh = value


# ---------------------------------------------------------------------------
# MQTT-Callbacks
# ---------------------------------------------------------------------------

def make_on_connect(
    topic_map: dict,
    log: logging.Logger,
):
    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("MQTT-Verbindung fehlgeschlagen: %s", reason_code)
            return
        log.info("MQTT verbunden. Subscribing auf %d Topics.", len(topic_map))
        for topic in topic_map:
            client.subscribe(topic)
            log.debug("Subscribed: %s", topic)
    return on_connect


def make_on_message(
    topic_map: dict,
    lock: threading.Lock,
    log: logging.Logger,
):
    def on_message(client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode("utf-8", errors="replace").strip()
        entries = topic_map.get(topic)
        if not entries:
            return

        now = time.time()
        with lock:
            for state, field_name in entries:
                _update_state(state, field_name, payload, log)
            # last_update und UDP-Senden einmal pro Meter (nicht pro Feld)
            sent_serials: set[int] = set()
            for state, _ in entries:
                state.last_update = now
                state.timed_out = False
                if state.serial not in sent_serials:
                    state.sender.send(state.power_w, state.energy_wh)
                    sent_serials.add(state.serial)
                    log.info(
                        "[serial=%d] %.1f W | %.3f kWh  (topic: %s)",
                        state.serial, state.power_w, state.energy_wh / 1000, topic,
                    )
    return on_message


# ---------------------------------------------------------------------------
# 0x6065 Discovery-Antwort
# ---------------------------------------------------------------------------

def build_discovery_response(
    dest_susyid: int,
    dest_serial: int,
    src_susyid: int,
    src_serial: int,
    request_data: bytes,
) -> bytes:
    """Baut eine 0x6065 Discovery-Antwort (58 Bytes, identische Struktur wie Anfrage,
    Quelle und Ziel getauscht, alles little-endian wie im SHM-Paket)."""
    buf = bytearray(58)
    buf[0:4]   = b"SMA\x00"
    buf[4:6]   = b"\x00\x04"
    buf[6:8]   = b"\x02\xa0"
    buf[8:12]  = b"\x00\x00\x00\x01"
    buf[12:14] = b"\x00\x26"   # inner_len = 38
    buf[14:16] = b"\x00\x10"
    buf[16:18] = b"\x60\x65"   # protocol 0x6065
    buf[18:20] = request_data[18:20]   # gleicher Command wie Anfrage
    # Ziel: SHM (little-endian)
    buf[20] = dest_susyid & 0xFF;       buf[21] = (dest_susyid >> 8) & 0xFF
    buf[22] = dest_serial & 0xFF;       buf[23] = (dest_serial >> 8) & 0xFF
    buf[24] = (dest_serial >> 16) & 0xFF; buf[25] = (dest_serial >> 24) & 0xFF
    # Counter spiegeln
    buf[26:28] = request_data[26:28]
    # Quelle: unser Gerät (little-endian)
    buf[28] = src_susyid & 0xFF;        buf[29] = (src_susyid >> 8) & 0xFF
    buf[30] = src_serial & 0xFF;        buf[31] = (src_serial >> 8) & 0xFF
    buf[32] = (src_serial >> 16) & 0xFF; buf[33] = (src_serial >> 24) & 0xFF
    # Restliche Bytes aus der Anfrage spiegeln
    if len(request_data) >= 54:
        buf[34:54] = request_data[34:54]
    buf[54:58] = b"\x00\x00\x00\x00"   # End-Marker
    return bytes(buf)


# ---------------------------------------------------------------------------
# UDP-Listener – antwortet auf 0x6065 Discovery-Anfragen des SHM
# ---------------------------------------------------------------------------

def udp_listener_thread(port: int, states: list, log: logging.Logger) -> None:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", port))
        log.info("UDP-Listener gestartet auf Port %d", port)
        last_response: dict[str, float] = {}   # addr -> Zeitstempel letzte Antwort
        while True:
            data, addr = sock.recvfrom(4096)
            if len(data) < 20 or data[0:4] != b"SMA\x00":
                continue
            proto = (data[16] << 8) | data[17]
            if proto != 0x6065:
                log.debug("UDP-EINGEHEND (unbekannt) von %s:%d  proto=0x%04X  len=%d",
                          addr[0], addr[1], proto, len(data))
                continue
            if len(data) < 34:
                continue
            # Rate-Limit: max 1 Antwort pro Sekunde pro Quell-IP
            now = time.time()
            if now - last_response.get(addr[0], 0) < 1.0:
                continue
            last_response[addr[0]] = now
            # SHM SusyID und Serial aus der Anfrage (little-endian, Offset 28-33)
            shm_susyid = data[28] | (data[29] << 8)
            shm_serial = data[30] | (data[31] << 8) | (data[32] << 16) | (data[33] << 24)
            log.info("Discovery vom SHM (SusyID=%d serial=%d) – sende Antwort für %d Gerät(e)",
                     shm_susyid, shm_serial, len(states))
            for state in states:
                resp = build_discovery_response(
                    dest_susyid=shm_susyid,
                    dest_serial=shm_serial,
                    src_susyid=SUSYID,
                    src_serial=state.serial,
                    request_data=data,
                )
                sock.sendto(resp, addr)
                log.info("  → Discovery-Antwort serial=%d SusyID=%d gesendet an %s",
                         state.serial, SUSYID, addr[0])
    except Exception as exc:
        log.error("UDP-Listener Fehler: %s", exc)


# ---------------------------------------------------------------------------
# Watchdog-Thread
# ---------------------------------------------------------------------------

def watchdog_thread(
    states: list[EMeterState],
    lock: threading.Lock,
    timeout_s: float,
    log: logging.Logger,
) -> None:
    tick = 0
    while True:
        time.sleep(1)
        tick += 1
        now = time.time()
        with lock:
            for state in states:
                if state.is_static:
                    # Leistung um ±1 W oszillieren damit der SHM den Wert als "neu" erkennt
                    send_power = state.power_w + (1.0 if tick % 2 else -1.0)
                    state.energy_wh += state.power_w / 3600.0
                    state.sender.send(send_power, state.energy_wh)
                    log.debug("[serial=%d] heartbeat %.1f W  %.3f kWh", state.serial, send_power, state.energy_wh / 1000)
                    continue
                else:
                    age = now - state.last_update
                    if age > timeout_s and not state.timed_out:
                        log.warning(
                            "[serial=%d] Kein Update seit %.0f s → sende 0 W",
                            state.serial, age,
                        )
                        state.power_w = 0.0
                        state.timed_out = True
                state.sender.send(state.power_w, state.energy_wh)
                log.debug("[serial=%d] heartbeat %.1f W  %.3f kWh", state.serial, state.power_w, state.energy_wh / 1000)


# ---------------------------------------------------------------------------
# Einstiegspunkt
# ---------------------------------------------------------------------------

def main() -> None:
    # .env-Datei laden falls vorhanden (lokal entwickeln ohne ENV-Export)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    parser = argparse.ArgumentParser(
        description="MQTT → SMA EMETER Speedwire Bridge",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default="/app/config.yaml",
        help="Pfad zur YAML-Konfigurationsdatei",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log-Level (überschreibt config.yaml)",
    )
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    log_level = args.log_level or cfg["log_level"]

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("bridge")

    log.info("=== MQTT → SMA EMETER Bridge ===")
    log.info("  MQTT broker : %s:%d", cfg["mqtt"]["broker"], cfg["mqtt"]["port"])
    log.info("  MQTT user   : %s", cfg["mqtt"]["username"] or "(none)")
    log.info("  Timeout     : %s s", cfg["mqtt"]["timeout"])
    log.info("  Interface   : %s", cfg["interface"] or "(default)")
    log.info("  Log level   : %s", log_level)

    # Meter-States und Sender erstellen
    states: list[EMeterState] = []
    for em in cfg["emeters"]:
        sender = EMETERSender(
            serial=em["serial"],
            interface=cfg["interface"],
        )
        state = EMeterState(
            serial=em["serial"],
            topic_power=em.get("topic_power", ""),
            topic_power_path=jparse(em["topic_power_path"]) if em.get("topic_power_path") else None,
            topic_energy_total=em.get("topic_energy_total"),
            topic_energy_total_path=jparse(em["topic_energy_total_path"]) if em.get("topic_energy_total_path") else None,
            topic_energy_total_unit=em.get("topic_energy_total_unit", "Wh"),
            power_w=float(em.get("static_power_w", 0.0)),
            energy_wh=float(em.get("static_energy_wh", 0.0)),
            is_static="static_power_w" in em,
            sender=sender,
        )
        states.append(state)
        if em.get("static_power_w") is not None:
            log.info("  Meter serial=%d: STATIC %.1f W / %.1f Wh  [SusyID=%d]",
                     state.serial, state.power_w, state.energy_wh, SUSYID)
        else:
            log.info(
                "  Meter serial=%d: power=%s%s  energy=%s%s  unit=%s  [SusyID=%d]",
                state.serial,
                state.topic_power,
                f" (path: {em['topic_power_path']})" if em.get("topic_power_path") else "",
                state.topic_energy_total or "(none)",
                f" (path: {em['topic_energy_total_path']})" if em.get("topic_energy_total_path") else "",
                state.topic_energy_total_unit,
                SUSYID,
            )

    lock = threading.Lock()
    topic_map = build_topic_map(states, log)

    # Watchdog-Daemon starten
    t = threading.Thread(
        target=watchdog_thread,
        args=(states, lock, cfg["mqtt"]["timeout"], log),
        daemon=True,
    )
    t.start()

    # UDP-Listener starten (antwortet auf 0x6065 Discovery-Anfragen des SHM)
    tl = threading.Thread(
        target=udp_listener_thread,
        args=(SMA_EMETER_UDP_PORT, states, log),
        daemon=True,
    )
    tl.start()

    # MQTT-Client aufbauen
    client = mqtt.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id="",
    )
    if cfg["mqtt"]["username"]:
        client.username_pw_set(cfg["mqtt"]["username"], cfg["mqtt"]["password"])

    client.on_connect = make_on_connect(topic_map, log)
    client.on_message = make_on_message(topic_map, lock, log)

    client.connect(cfg["mqtt"]["broker"], cfg["mqtt"]["port"], keepalive=60)

    log.info("MQTT-Loop gestartet (broker=%s:%d) …", cfg["mqtt"]["broker"], cfg["mqtt"]["port"])
    try:
        client.loop_forever(retry_first_connection=True)
    except KeyboardInterrupt:
        log.info("Unterbrochen.")
    finally:
        for state in states:
            state.sender.close()
        log.info("Sockets geschlossen.")


if __name__ == "__main__":
    main()
