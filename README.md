# mqtt-sma-emeter

MQTT → SMA Speedwire EMETER Bridge.

Abonniert MQTT-Topics und sendet die empfangenen Leistungs- und Energiewerte als
SMA Speedwire EMETER UDP-Multicast-Pakete. Der SMA Home Manager 2.0 erkennt die
konfigurierten Meter als virtuelle Erzeuger und berücksichtigt sie beim
Überschussladen der SMA Wallbox.

Mehrere virtuelle EMETER mit unterschiedlichen Seriennummern werden gleichzeitig
unterstützt. Die Quelle kann ein plain-float-Payload oder ein JSON-Payload mit
JSON-Path-Extraktion sein.

## Voraussetzungen

- SMA Home Manager 2.0 (Firmware ≥ 1.07)
- MQTT-Broker im lokalen Netz (z. B. Mosquitto)
- Docker **oder** Python 3.10+

## Schnellstart mit Docker

```bash
# 1. config.yaml anpassen (Broker-IP, Topics, Seriennummern)

# 2. Container starten
docker compose up -d

# 3. Logs beobachten
docker compose logs -f
```

> **Wichtig:** `network_mode: host` in `docker-compose.yml` ist **Pflicht**.
> Ohne Host-Netzwerk kann der Container keine UDP-Multicast-Pakete ins lokale
> Netz senden, die der SMA Home Manager empfängt.

## Konfiguration

Die gesamte Konfiguration erfolgt über eine YAML-Datei, die als Volume in den
Container gemountet wird:

```yaml
# config.yaml
mqtt:
  broker: 192.168.1.10   # IP oder Hostname des MQTT-Brokers
  port: 1883             # Standard: 1883
  username: ""           # leer lassen wenn kein Auth
  password: ""
  timeout: 300           # Sekunden ohne Update → 0 W senden (pro Meter)

interface: ""            # Multicast-Quell-Interface: IP ("192.168.1.5") oder Name ("eth0")
log_level: INFO          # DEBUG | INFO | WARNING | ERROR

emeters:
  # Meter 1: Power und Energie als separate Topics, plain float
  - serial: 900000002
    topic_power: solar/ac/power
    topic_energy_total: solar/ac/yieldtotal
    topic_energy_total_unit: kWh     # "Wh" (default) oder "kWh"

  # Meter 2: Tasmota – beide Werte in einer JSON-Nachricht
  - serial: 900000003
    topic_power: tele/tasmota_0FF8CC/SENSOR
    topic_power_path: "$.ENERGY.Power"
    topic_energy_total: tele/tasmota_0FF8CC/SENSOR
    topic_energy_total_path: "$.ENERGY.Total"
    topic_energy_total_unit: kWh
```

### Meter-Felder

| Feld | Pflicht | Beschreibung |
|------|---------|--------------|
| `serial` | ja | Virtuelle Seriennummer (32-bit, im LAN eindeutig) |
| `topic_power` | ja | MQTT-Topic für die aktuelle Leistung in Watt |
| `topic_power_path` | nein | JSON-Path zum Wert im Payload (z. B. `$.ENERGY.Power`) |
| `topic_energy_total` | nein | MQTT-Topic für den kumulierten Energiezähler |
| `topic_energy_total_path` | nein | JSON-Path zum Wert im Payload |
| `topic_energy_total_unit` | nein | Einheit des Energiewerts: `Wh` (default) oder `kWh` |

### Payload-Formate

**Plain float** (kein `_path` angegeben):
```
Topic: solar/ac/power
Payload: 1234.5
```

**JSON mit JSON-Path**:
```
Topic: tele/tasmota/SENSOR
Payload: {"ENERGY": {"Power": 250, "Total": 1.5}}
Path:    $.ENERGY.Power
```

## MQTT-Credentials per Umgebungsvariable

Für sensible Daten (Broker-Adresse, Zugangsdaten) können alle MQTT-Parameter
per Umgebungsvariable gesetzt oder überschrieben werden. ENV-Variablen haben
**Priorität** gegenüber `config.yaml`.

| ENV-Variable | Entspricht | Beispiel |
|-------------|------------|---------|
| `MQTT_BROKER` | `mqtt.broker` | `192.168.1.10` |
| `MQTT_PORT` | `mqtt.port` | `1883` |
| `MQTT_USERNAME` | `mqtt.username` | `myuser` |
| `MQTT_PASSWORD` | `mqtt.password` | `secret` |
| `MQTT_TIMEOUT` | `mqtt.timeout` | `300` |

**Beispiel `docker-compose.yml`** mit Credentials über ENV:

```yaml
services:
  mqtt-sma-bridge:
    image: michelmu/mqtt-sma-emeter:latest
    container_name: mqtt-sma-bridge
    restart: unless-stopped
    network_mode: host
    volumes:
      - ./config.yaml:/app/config.yaml:ro
    environment:
      MQTT_BROKER: 192.168.1.10
      MQTT_USERNAME: myuser
      MQTT_PASSWORD: secret
      LOG_LEVEL: INFO
```

So bleibt `config.yaml` frei von Passwörtern und kann bedenkenlos im Git
eingecheckt werden.

## Lokales Testen mit Python und venv

### 1. Virtuelle Umgebung einrichten

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. config.yaml anpassen

Broker-IP und Topics in `config.yaml` eintragen. Für einen lokalen
Mosquitto-Testbroker genügt:

```yaml
mqtt:
  broker: localhost
  timeout: 30
```

### 3. Bridge starten

```bash
python3 mqtt_sma_bridge.py --config ./config.yaml --log-level DEBUG
```

### 4. Test-Nachrichten senden

In einem zweiten Terminal (ebenfalls mit aktiviertem venv oder mit
system-mosquitto):

```bash
# Plain-float Payload (Meter 1)
mosquitto_pub -t solar/ac/power -m 1234.5
mosquitto_pub -t solar/ac/yieldtotal -m 987.6

# JSON-Payload (Meter 2)
mosquitto_pub -t tele/tasmota_0FF8CC/SENSOR \
  -m '{"ENERGY":{"Power":300,"Total":1.5}}'
```

Im Bridge-Log sollte dann erscheinen:

```
12:34:56  INFO      [serial=900000002] 1234.5 W | 0.988 kWh  (topic: solar/ac/power)
12:34:57  INFO      [serial=900000003] 300.0 W | 1500.000 kWh  (topic: tele/tasmota_0FF8CC/SENSOR)
```

### 5. Timeout testen

Nach `mqtt.timeout` Sekunden ohne neue Nachricht erscheint:

```
12:35:30  WARNING   [serial=900000002] Kein Update seit 30 s → sende 0 W
```

### Optionale CLI-Argumente

| Argument | Beschreibung |
|----------|-------------|
| `--config PATH` | Pfad zur YAML-Datei (Standard: `/app/config.yaml`) |
| `--log-level LEVEL` | Log-Level überschreiben (DEBUG / INFO / WARNING / ERROR) |

## Timeout-Verhalten

Empfängt ein Meter länger als `mqtt.timeout` Sekunden keine MQTT-Nachricht,
sendet die Bridge **0 W** an den SMA Home Manager. Der Energiezähler bleibt
dabei unverändert (kein Reset). So werden veraltete Werte nach einem
Geräteausfall automatisch gelöscht.

## Technische Details

- **Protokoll:** SMA Speedwire EMETER v1.0 (UDP-Multicast `239.12.255.254:9522`)
- **Trigger:** UDP-Paket wird bei jedem eingehenden MQTT-Update gesendet (kein Timer)
- **Nebenläufigkeit:** MQTT-Callback-Thread + Watchdog-Daemon-Thread, geschützt durch `threading.Lock`
- **Wiederverbindung:** paho-mqtt verbindet sich bei Verbindungsverlust automatisch neu

## Seriennummern

Jeder virtuelle EMETER benötigt eine im LAN eindeutige 32-bit-Seriennummer.
Verwende Werte im Bereich `900000001`–`999999999`, die nicht mit echten
SMA-Geräten kollidieren.

## Lizenz

MIT
