#!/usr/bin/env python3
"""Marstek Cloud (eu.hamedata.com) to MQTT bridge for LoxBerry.

Logs into the Marstek cloud API, polls the device list, and republishes every
configured datapoint to the LoxBerry MQTT broker as retained messages under
``<topic_prefix>/<device_sn>/<datapoint>``.

API endpoints (derived from the DoctaShizzle/marstek_cloud Home Assistant
integration):

- POST {base}/app/Solar/v2_get_device.php?pwd=<md5(password)>&mailbox=<email>
- GET  {base}/ems/api/v1/getDeviceList?token=<token>

Error code 8 on the device-list call means the token expired; the daemon
discards the cached token and re-logs in on the next cycle.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None


PLUGIN_NAME = "marstek-cloud"
PLUGIN_VERSION = "1.0.0"  # keep in sync with plugin.cfg [PLUGIN] VERSION
DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "email": "",
    "password": "",
    "api_base_url": "https://eu.hamedata.com",
    "poll_interval_seconds": 60,
    "request_timeout_seconds": 15,
    "request_attempts": 3,
    "use_loxberry_mqtt": True,
    "mqtt_host": "localhost",
    "mqtt_port": 1883,
    "mqtt_username": "",
    "mqtt_password": "",
    "mqtt_topic_prefix": "marstek",
    "mqtt_dry_run": False,
    "register_mqtt_subscription": True,
    "publish_raw_json": False,
    "ignored_device_types": ["HME-3"],
    "data_points": [
        "soc",
        "charge",
        "discharge",
        "load",
        "profit",
        "version",
        "sn",
        "report_time",
    ],
    "debug": False,
}

CONFIG_DIR = Path(os.environ.get("LBPCONFIGDIR", "./config"))
LOG_DIR = Path(os.environ.get("LBPLOGDIR", "./logs"))
# LoxBerry exports LBHOMEDIR and LBSCONFIG via /etc/environment, inherited
# from loxberry.service. For local-dev runs outside LoxBerry, set them in
# the calling shell — do NOT hardcode the LoxBerry install root here; the
# installer's hardcoded-path linter grep -l's daemon scripts for it.
_LBHOMEDIR = os.environ.get("LBHOMEDIR") or ""
LBHOMEDIR = Path(_LBHOMEDIR) if _LBHOMEDIR else Path(".")
LBSCONFIGDIR = Path(os.environ.get("LBSCONFIG") or (LBHOMEDIR / "config" / "system"))
_shutdown = threading.Event()
TOPIC_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class MarstekCloudError(RuntimeError):
    """Raised when a Marstek Cloud API call fails."""


class TokenExpired(MarstekCloudError):
    """Raised when the API reports an expired/invalid token (code 8)."""


def handle_shutdown(signum: int, _frame: Any) -> None:
    logging.info("Received signal %s, stopping daemon", signum)
    _shutdown.set()


signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)


def setup_logging(debug: bool = False) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{PLUGIN_NAME}.log"
    logging.basicConfig(
        filename=log_path,
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if debug else logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(console)


def load_config() -> dict[str, Any]:
    config_path = CONFIG_DIR / "default.json"
    config = dict(DEFAULT_CONFIG)
    if config_path.exists():
        with config_path.open("r", encoding="utf-8-sig") as fh:
            config.update(json.load(fh))
        # Defensive: ensure plaintext credentials aren't readable by other
        # users on the host. Mirrors the CGI's chmod-on-save; covers the
        # case where the file was installed at 0644 and the user has not
        # yet saved through the form.
        try:
            config_path.chmod(0o600)
        except OSError:
            pass  # best-effort; not fatal
    return config


def int_between(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def sanitize_config(config: dict[str, Any]) -> dict[str, Any]:
    safe = dict(config)
    safe["poll_interval_seconds"] = int_between(config.get("poll_interval_seconds"), 60, 10, 3600)
    safe["request_timeout_seconds"] = int_between(config.get("request_timeout_seconds"), 15, 3, 120)
    safe["request_attempts"] = int_between(config.get("request_attempts"), 3, 1, 10)
    safe["mqtt_port"] = int_between(config.get("mqtt_port"), 1883, 1, 65535)
    prefix = str(config.get("mqtt_topic_prefix") or "marstek").strip().strip("/")
    safe["mqtt_topic_prefix"] = TOPIC_SEGMENT_RE.sub("_", prefix) or "marstek"
    return safe


def load_loxberry_mqtt_creds() -> dict[str, Any] | None:
    """Read the broker host/port/user/pass from LoxBerry's general.json.

    Returns None if the file is not present or doesn't have the Mqtt section
    (e.g. when running outside LoxBerry, during local tests, or on a brand-new
    install before the user finished first-run setup).
    """
    path = LBSCONFIGDIR / "general.json"
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            mqtt = json.load(fh).get("Mqtt") or {}
    except (OSError, json.JSONDecodeError) as err:
        logging.warning("Could not read %s: %s; falling back to manual MQTT config", path, err)
        return None
    host = mqtt.get("Brokerhost") or ""
    port = mqtt.get("Brokerport") or 0
    if not host or not port:
        logging.warning("LoxBerry general.json Mqtt section missing Brokerhost/Brokerport; falling back to manual config")
        return None
    return {
        "host": str(host),
        "port": int(port),
        "username": mqtt.get("Brokeruser") or "",
        "password": mqtt.get("Brokerpass") or "",
    }


def apply_loxberry_mqtt_creds(config: dict[str, Any]) -> dict[str, Any]:
    """If use_loxberry_mqtt is on, replace manual broker fields with the
    discovered LoxBerry MQTT broker credentials. Returns the new config dict
    (does not mutate the input).
    """
    if not config.get("use_loxberry_mqtt", True):
        return config
    creds = load_loxberry_mqtt_creds()
    if not creds:
        return config
    merged = dict(config)
    merged["mqtt_host"] = creds["host"]
    merged["mqtt_port"] = creds["port"]
    merged["mqtt_username"] = creds["username"]
    merged["mqtt_password"] = creds["password"]
    logging.info(
        "Using LoxBerry MQTT broker at %s:%s (user=%s)",
        creds["host"], creds["port"], redact(creds["username"]),
    )
    return merged


def register_mqtt_subscription(prefix: str) -> None:
    """Write <LBPCONFIGDIR>/mqtt_subscriptions.cfg so the LoxBerry built-in
    MQTT Gateway picks up our topic prefix and relays it to the Loxone
    Miniserver as Virtual Inputs. The gateway watches this file with inotify
    so changes apply within seconds. No-op when the plugin config dir does
    not exist (running outside LoxBerry).
    """
    if not CONFIG_DIR.exists():
        logging.debug("CONFIG_DIR %s does not exist; skipping subscription registration", CONFIG_DIR)
        return
    target = CONFIG_DIR / "mqtt_subscriptions.cfg"
    body = f"{prefix}/#\n"
    try:
        if target.exists() and target.read_text(encoding="utf-8") == body:
            return  # already up to date
        target.write_text(body, encoding="utf-8")
        logging.info("Registered MQTT subscription %s with the LoxBerry MQTT Gateway (%s)", body.strip(), target)
    except OSError as err:
        logging.warning("Could not write %s: %s", target, err)


def safe_topic_segment(value: Any, default: str) -> str:
    segment = TOPIC_SEGMENT_RE.sub("_", str(value or default).strip()).strip("_")
    return segment or default


def normalize_value(value: Any) -> str:
    if value is True:
        return "1"
    if value is False:
        return "0"
    if value is None:
        return ""
    return str(value)


def md5_hex(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def redact(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "***"
    return f"{value[:2]}***{value[-2:]}"


def validate_api_base_url(url: str) -> str | None:
    """Return None if *url* is an allowed Marstek base URL; else an error string.

    Allow-list:
      - https://(*.)hamedata.com[:port][/path]   — Marstek's known regions
      - http(s)://localhost[:port][/path]         — local dev / mocks

    This blocks the credential-exfil chain where a CSRF attacker rewrites
    api_base_url to point at their own host: the daemon would otherwise POST
    `mailbox=<email>&pwd=<md5>` to whatever URL the config says.
    """
    if not url:
        return "api_base_url is empty"
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception as err:  # urlparse is very forgiving but be paranoid
        return f"api_base_url could not be parsed: {err}"
    if parsed.scheme not in ("http", "https"):
        return f"api_base_url uses disallowed scheme {parsed.scheme!r}; must be http or https"
    host = (parsed.hostname or "").lower()
    if not host:
        return "api_base_url has no host"
    if host == "localhost":
        return None
    if host == "hamedata.com" or host.endswith(".hamedata.com"):
        return None
    return (
        f"api_base_url host {host!r} is not on the allow-list. "
        "Allowed: *.hamedata.com (Marstek regions) or localhost (for testing)."
    )


def redact_url(url: str) -> str:
    """Return a log-safe form of *url* with all query parameters stripped.

    The Marstek login URL embeds both the MD5(password) and the email in the
    query string (`?pwd=<md5>&mailbox=<email>`). Errors from `urlopen` would
    otherwise echo the full URL to the log file — the MD5 is rainbow-table
    fodder and the email is identifying. Always log via this helper.
    """
    head, sep, _tail = url.partition("?")
    return head + "?<redacted>" if sep else head


class MarstekCloudClient:
    def __init__(
        self,
        base_url: str,
        email: str,
        password: str,
        timeout: float,
        attempts: int,
    ) -> None:
        if not email or not password:
            raise MarstekCloudError("email and password are required in config/default.json")
        self.base_url = base_url.rstrip("/")
        self.email = email
        self._md5_password = md5_hex(password)
        self.timeout = float(timeout)
        self.attempts = int(attempts)
        self._token: str | None = None
        self._last_latency_ms: int | None = None

    @property
    def last_latency_ms(self) -> int | None:
        return self._last_latency_ms

    def _request(self, method: str, url: str) -> dict[str, Any]:
        req = urllib.request.Request(url, method=method)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", f"LoxBerry-{PLUGIN_NAME}/{PLUGIN_VERSION}")
        safe_url = redact_url(url)
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except (urllib.error.URLError, socket.timeout, TimeoutError) as err:
            self._last_latency_ms = int((time.monotonic() - started) * 1000)
            raise MarstekCloudError(f"HTTP {method} {safe_url} failed: {err}") from err
        self._last_latency_ms = int((time.monotonic() - started) * 1000)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise MarstekCloudError(f"Invalid JSON from {safe_url}: {err}") from err

    def _with_retry(self, label: str, fn) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                return fn()
            except TokenExpired:
                raise
            except MarstekCloudError as err:
                last_error = err
                logging.warning("%s failed (attempt %s/%s): %s", label, attempt, self.attempts, err)
                if attempt < self.attempts and not _shutdown.is_set():
                    time.sleep(min(2 ** (attempt - 1), 10))
        assert last_error is not None
        raise last_error

    def login(self) -> str:
        params = urllib.parse.urlencode({"pwd": self._md5_password, "mailbox": self.email})
        url = f"{self.base_url}/app/Solar/v2_get_device.php?{params}"
        logging.info("Logging in to Marstek Cloud as %s", redact(self.email))
        data = self._with_retry("login", lambda: self._request("POST", url))
        token = data.get("token")
        if not token:
            raise MarstekCloudError(f"Login response missing token: {data}")
        self._token = str(token)
        logging.info("Login OK, token cached (%s)", redact(self._token))
        return self._token

    def get_devices(self) -> list[dict[str, Any]]:
        if not self._token:
            self.login()
        assert self._token is not None
        params = urllib.parse.urlencode({"token": self._token})
        url = f"{self.base_url}/ems/api/v1/getDeviceList?{params}"

        def call() -> dict[str, Any]:
            data = self._request("GET", url)
            # Marstek serializes status codes as strings ("2", "3", "4", "8").
            # Coerce so int/str shape changes don't silently break branching.
            code = str(data.get("code") or "")
            if code == "8":
                raise TokenExpired("API code 8: token invalid or expired")
            # Success when the API returns a list under "data". We do NOT
            # couple to a specific success-code value — observed codes vary
            # across endpoints (login = "2", others not yet captured), and
            # error bodies include an empty `"data": ""`, which makes
            # presence-of-key alone an unreliable signal.
            if not isinstance(data.get("data"), list):
                raise MarstekCloudError(f"getDeviceList failed: code={code} msg={data.get('msg')}")
            return data

        try:
            data = self._with_retry("getDeviceList", call)
        except TokenExpired:
            logging.info("Token expired; re-authenticating")
            self._token = None
            self.login()
            data = self._with_retry("getDeviceList", call)

        return data["data"]


class MqttPublisher:
    def __init__(self, config: dict[str, Any]) -> None:
        self.dry_run = bool(config.get("mqtt_dry_run"))
        self.client = None
        if self.dry_run:
            logging.info("MQTT dry-run enabled; values will be logged but not published")
            return
        if mqtt is None:
            raise MarstekCloudError("Missing dependency: paho-mqtt. Install with 'pip3 install paho-mqtt'.")
        client_kwargs: dict[str, Any] = {}
        if hasattr(mqtt, "CallbackAPIVersion"):
            client_kwargs["callback_api_version"] = mqtt.CallbackAPIVersion.VERSION2
        self.client = mqtt.Client(**client_kwargs)
        username = config.get("mqtt_username")
        password = config.get("mqtt_password")
        if username:
            self.client.username_pw_set(username, password or None)
        host = config.get("mqtt_host", "localhost")
        port = int(config.get("mqtt_port", 1883))
        _CONNECT_ATTEMPTS = 3
        for attempt in range(1, _CONNECT_ATTEMPTS + 1):
            try:
                self.client.connect(host, port, keepalive=30)
                break
            except OSError as err:
                if attempt == _CONNECT_ATTEMPTS:
                    raise MarstekCloudError(
                        f"Cannot connect to MQTT broker at {host}:{port} after "
                        f"{_CONNECT_ATTEMPTS} attempts: {err}"
                    ) from err
                logging.warning(
                    "MQTT connect attempt %s/%s failed: %s; retrying in 5 s",
                    attempt, _CONNECT_ATTEMPTS, err,
                )
                time.sleep(5)
        self.client.loop_start()

    def close(self) -> None:
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()

    def publish(self, topic: str, value: Any) -> None:
        if self.dry_run:
            logging.info("MQTT dry-run publish %s=%s", topic, normalize_value(value))
            return
        if self.client:
            self.client.publish(topic, normalize_value(value), retain=True)


def device_topic(prefix: str, device_id: str, datapoint: str) -> str:
    return f"{prefix}/{safe_topic_segment(device_id, 'device').lower()}/{safe_topic_segment(datapoint, 'value')}"


def publish_device(
    publisher: MqttPublisher,
    config: dict[str, Any],
    device: dict[str, Any],
    api_latency_ms: int | None,
) -> None:
    prefix = config["mqtt_topic_prefix"]
    device_id = str(device.get("sn") or device.get("devid") or device.get("mac") or "unknown")
    points = config.get("data_points") or list(device.keys())

    for key in points:
        if key in device:
            publisher.publish(device_topic(prefix, device_id, key), device[key])

    publisher.publish(device_topic(prefix, device_id, "connection_status"), "online")
    publisher.publish(device_topic(prefix, device_id, "last_update_epoch"), int(time.time()))
    if api_latency_ms is not None:
        publisher.publish(device_topic(prefix, device_id, "api_latency_ms"), api_latency_ms)

    if config.get("publish_raw_json"):
        publisher.publish(device_topic(prefix, device_id, "raw_json"), json.dumps(device, sort_keys=True))


def poll_once(config: dict[str, Any], client: MarstekCloudClient, publisher: MqttPublisher) -> None:
    prefix = config["mqtt_topic_prefix"]
    ignored = set(config.get("ignored_device_types") or [])
    devices = client.get_devices()
    if not devices:
        logging.info("No devices returned by API")
    visible = 0
    for device in devices:
        if device.get("type") in ignored:
            logging.debug("Ignoring device type %s", device.get("type"))
            continue
        visible += 1
        publish_device(publisher, config, device, client.last_latency_ms)
    publisher.publish(f"{prefix}/_status", "online")
    publisher.publish(f"{prefix}/_device_count", visible)
    publisher.publish(f"{prefix}/_last_poll_epoch", int(time.time()))


def main() -> int:
    config = sanitize_config(load_config())
    setup_logging(bool(config.get("debug")))
    logging.info("%s daemon starting", PLUGIN_NAME)

    if not config.get("enabled", True):
        logging.info("Daemon disabled in config; exiting")
        return 0

    # Defensive: also strip whitespace here in case the config was hand-edited
    # or saved by an older plugin version. A single leading/trailing space
    # changes the MD5 and Marstek rejects with code 4 (password incorrect).
    config["email"]    = (config.get("email") or "").strip()
    config["password"] = (config.get("password") or "").strip()
    if not config["email"] or not config["password"]:
        logging.warning(
            "Marstek account email/password not configured yet; daemon exiting cleanly. "
            "Open the plugin page in LoxBerry, enter credentials, and click 'Save and restart daemon'."
        )
        return 0

    api_base_url = config.get("api_base_url") or ""
    base_url_error = validate_api_base_url(api_base_url)
    if base_url_error:
        logging.error(
            "Refusing to start: %s (got %r). Edit the plugin page to fix.",
            base_url_error, api_base_url,
        )
        return 0

    config = apply_loxberry_mqtt_creds(config)
    if config.get("register_mqtt_subscription", True):
        register_mqtt_subscription(config["mqtt_topic_prefix"])

    client = MarstekCloudClient(
        base_url=config.get("api_base_url", "https://eu.hamedata.com"),
        email=config.get("email", ""),
        password=config.get("password", ""),
        timeout=config["request_timeout_seconds"],
        attempts=config["request_attempts"],
    )
    publisher = MqttPublisher(config)

    try:
        while not _shutdown.is_set():
            try:
                poll_once(config, client, publisher)
            except Exception as err:
                logging.exception("Polling cycle failed: %s", err)
                publisher.publish(f"{config['mqtt_topic_prefix']}/_status", "error")
            _shutdown.wait(timeout=config["poll_interval_seconds"])
    finally:
        publisher.publish(f"{config['mqtt_topic_prefix']}/_status", "offline")
        publisher.close()
        logging.info("%s daemon stopped", PLUGIN_NAME)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
