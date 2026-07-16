#!/usr/bin/env python3
"""Watch Junos for commits and archive each commit diff into MongoDB.

Two detection back-ends are supported (selectable via ``watcher.mode``):

* ``syslog`` (default) -- listen on a UDP syslog port and keep only messages
  containing ``UI_COMMIT_COMPLETED``.
* ``gnmi`` -- open a gNMI ``on_change`` subscription to each router on the path
  ``/junos/events/event[id=UI_COMMIT_PROGRESS]`` and react when a
  ``commit complete`` message is streamed.
* ``both`` -- run the syslog listener and the gNMI subscriptions together.

Common flow (regardless of back-end)
------------------------------------
1. Detect a successful commit on a router.
2. Identify the router (syslog hostname / packet source, or the configured
   gNMI router name/hostname).
3. Open a **non-blocking** (async) NETCONF session with ``juniper_api`` and pull
   the diff of the latest commit (``show_diff(mode="committed")``) plus the
   device model and Junos version (``show version``). The blocking PyEZ work
   runs in a thread-pool executor so the listener never stalls.
4. Store a JSON document ``{source, date, diff, model, version}`` in MongoDB,
   ready to be rendered later as a per-router timeline.

Run ``python commit_watcher.py --help`` for usage.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import os
import re
import signal
import threading
from typing import Any, Dict, List, Optional, Tuple

import yaml
from motor.motor_asyncio import AsyncIOMotorClient

from juniper_api import JuniperDevice, JuniperDeviceError

LOG = logging.getLogger("commit_watcher")

# Marker Junos writes to syslog when a commit finishes successfully.
COMMIT_MARKER = "UI_COMMIT_COMPLETED"

# gNMI on-change subscription path for Junos commit-progress events, and the
# message text streamed on that path when a commit has finished.
GNMI_COMMIT_PATH = "/junos/events/event[id=UI_COMMIT_PROGRESS]"
GNMI_COMMIT_MARKER = "commit complete"

# RFC 3164 (BSD) syslog: "<PRI>Mon DD HH:MM:SS hostname tag: message".
# The year is not part of the timestamp, so the current year is assumed.
_RFC3164_RE = re.compile(
    r"^(?:<(?P<pri>\d{1,3})>)?"
    r"(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+"
    r"(?P<host>\S+)\s+"
)

# RFC 5424 syslog: "<PRI>1 ISO-TIMESTAMP hostname app procid msgid ...".
_RFC5424_RE = re.compile(
    r"^(?:<(?P<pri>\d{1,3})>)?1\s+"
    r"(?P<ts>\S+)\s+"
    r"(?P<host>\S+)\s+"
)

_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


# --------------------------------------------------------------------------- #
# Syslog parsing
# --------------------------------------------------------------------------- #
class ParsedMessage:
    """A minimal, parsed syslog record."""

    __slots__ = ("hostname", "timestamp", "raw")

    def __init__(self, hostname: Optional[str], timestamp: dt.datetime, raw: str) -> None:
        self.hostname = hostname
        self.timestamp = timestamp
        self.raw = raw


def _parse_rfc3164_ts(ts: str) -> dt.datetime:
    """Parse a ``Mon DD HH:MM:SS`` timestamp, assuming the current year."""
    month_str, day_str, clock = ts.split(maxsplit=2)
    hour, minute, second = (int(part) for part in clock.split(":"))
    now = dt.datetime.now()
    parsed = dt.datetime(
        now.year, _MONTHS[month_str], int(day_str), hour, minute, second
    )
    # Handle a year boundary: a December message received in January.
    if parsed - now > dt.timedelta(days=1):
        parsed = parsed.replace(year=now.year - 1)
    return parsed


def parse_syslog(data: bytes) -> Optional[ParsedMessage]:
    """Parse a raw syslog datagram into a :class:`ParsedMessage`.

    Supports the two common Junos formats (RFC 3164 and RFC 5424). Returns
    ``None`` only when nothing usable can be extracted (the caller then falls
    back to the packet source and the receive time).
    """
    text = data.decode("utf-8", errors="replace").strip()

    match = _RFC5424_RE.match(text)
    if match:
        raw_ts = match.group("ts")
        try:
            timestamp = dt.datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        except ValueError:
            timestamp = dt.datetime.now()
        host = match.group("host")
        return ParsedMessage(None if host == "-" else host, timestamp, text)

    match = _RFC3164_RE.match(text)
    if match:
        try:
            timestamp = _parse_rfc3164_ts(match.group("ts"))
        except (ValueError, KeyError):
            timestamp = dt.datetime.now()
        return ParsedMessage(match.group("host"), timestamp, text)

    return ParsedMessage(None, dt.datetime.now(), text)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def _as_bool(value: Any) -> bool:
    """Coerce YAML/env values (``true``/``"1"``/``"yes"`` ...) to ``bool``."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class Config:
    """Runtime configuration loaded from a YAML file and/or environment."""

    def __init__(self, data: Dict[str, Any]) -> None:
        watcher = data.get("watcher") or {}
        # Detection back-end: "syslog" (default), "gnmi" or "both".
        self.mode: str = str(
            os.environ.get("WATCHER_MODE", watcher.get("mode", "syslog"))
        ).lower()

        syslog = data.get("syslog") or {}
        self.listen_host: str = os.environ.get(
            "SYSLOG_HOST", syslog.get("host", "0.0.0.0")
        )
        self.listen_port: int = int(
            os.environ.get("SYSLOG_PORT", syslog.get("port", 5514))
        )

        gnmi = data.get("gnmi") or {}
        self.gnmi_port: int = int(os.environ.get("GNMI_PORT", gnmi.get("port", 32767)))
        self.gnmi_user: Optional[str] = os.environ.get("GNMI_USER", gnmi.get("user"))
        self.gnmi_passwd: Optional[str] = os.environ.get(
            "GNMI_PASSWD", gnmi.get("passwd")
        )
        # Transport security. ``insecure`` uses plaintext (no TLS); otherwise
        # TLS is used and ``skip_verify`` disables certificate validation.
        self.gnmi_insecure: bool = _as_bool(
            os.environ.get("GNMI_INSECURE", gnmi.get("insecure", False))
        )
        self.gnmi_skip_verify: bool = _as_bool(
            os.environ.get("GNMI_SKIP_VERIFY", gnmi.get("skip_verify", True))
        )
        # Seconds to wait before re-subscribing after a dropped session.
        self.gnmi_reconnect_delay: int = int(
            os.environ.get("GNMI_RECONNECT_DELAY", gnmi.get("reconnect_delay", 10))
        )
        self.gnmi_routers: List[Dict[str, Any]] = list(gnmi.get("routers") or [])

        mongo = data.get("mongodb") or {}
        self.mongo_uri: str = os.environ.get(
            "MONGODB_URI", mongo.get("uri", "mongodb://localhost:27017")
        )
        self.mongo_db: str = os.environ.get(
            "MONGODB_DB", mongo.get("database", "junos_commits")
        )
        self.mongo_collection: str = os.environ.get(
            "MONGODB_COLLECTION", mongo.get("collection", "commit_diffs")
        )

        devices = data.get("devices") or {}
        self.device_defaults: Dict[str, Any] = devices.get("defaults") or {}
        # Connect to the syslog hostname instead of the packet source IP.
        self.connect_by_hostname: bool = bool(devices.get("connect_by_hostname", False))

    def credentials_for(
        self, hostname: Optional[str], source_ip: str
    ) -> Tuple[str, Dict[str, Any]]:
        """Return ``(connect_target, device_kwargs)`` for a router.

        The connection target is the source IP by default (most reliable), or
        the hostname when ``connect_by_hostname`` is set. The same default
        credentials/params are used for every device.
        """
        params: Dict[str, Any] = dict(self.device_defaults)
        target = hostname if (self.connect_by_hostname and hostname) else source_ip
        # ``host`` is passed explicitly to JuniperDevice; drop any stray copy.
        params.pop("host", None)
        return target, params


def load_config(path: Optional[str]) -> Config:
    data: Dict[str, Any] = {}
    if path:
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    return Config(data)


# --------------------------------------------------------------------------- #
# Commit archiving
# --------------------------------------------------------------------------- #
class CommitArchiver:
    """Fetches commit diffs over NETCONF and persists them to MongoDB."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._client = AsyncIOMotorClient(config.mongo_uri)
        self._collection = self._client[config.mongo_db][config.mongo_collection]
        # Serialise work per router so bursts of commits don't open several
        # concurrent sessions to the same device.
        self._locks: Dict[str, asyncio.Lock] = {}

    async def setup(self) -> None:
        """Create indexes that make per-router timeline queries efficient."""
        await self._collection.create_index([("source", 1), ("date", -1)])

    async def close(self) -> None:
        self._client.close()

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def handle_commit(self, message: ParsedMessage, source_ip: str) -> None:
        """Retrieve the latest commit diff for a router and store it."""
        hostname = message.hostname or source_ip
        target, device_kwargs = self._config.credentials_for(
            message.hostname, source_ip
        )

        async with self._lock_for(target):
            try:
                diff, model, version = await asyncio.to_thread(
                    self._fetch_commit_info, target, device_kwargs
                )
            except JuniperDeviceError as err:
                LOG.error("Failed to fetch commit info from %s: %s", target, err)
                return

            if not diff:
                LOG.info("Commit on %s produced an empty diff; storing anyway", hostname)

            document = {
                "source": hostname,
                "date": message.timestamp,
                "diff": diff or "",
                "model": model or "",
                "version": version or "",
                "received_at": dt.datetime.now(dt.timezone.utc),
                "source_ip": source_ip,
            }
            result = await self._collection.insert_one(document)
            LOG.info(
                "Stored commit diff for %s (date=%s, id=%s)",
                hostname,
                message.timestamp.isoformat(),
                result.inserted_id,
            )

    @staticmethod
    def _fetch_commit_info(
        target: str, device_kwargs: Dict[str, Any]
    ) -> Tuple[str, Optional[str], Optional[str]]:
        """Blocking PyEZ work: read the last commit diff plus device version.

        Opens a single NETCONF session and returns ``(diff, model, version)``
        where ``model`` is the hardware product model and ``version`` is the
        running Junos version (both from ``show version``). Runs in a worker
        thread via :func:`asyncio.to_thread`.
        """
        with JuniperDevice(host=target, **device_kwargs) as dev:
            diff = dev.show_diff(mode="committed")
            version_xml = dev.show("show version", fmt="xml")
            model = dev.select(version_xml, "product-model", first=True)
            version = dev.select(version_xml, "junos-version", first=True)
            return diff, model, version


# --------------------------------------------------------------------------- #
# UDP syslog server
# --------------------------------------------------------------------------- #
class SyslogProtocol(asyncio.DatagramProtocol):
    """Receives syslog datagrams and dispatches commit events."""

    def __init__(self, archiver: CommitArchiver) -> None:
        self._archiver = archiver
        self._tasks: "set[asyncio.Task[None]]" = set()

    def datagram_received(self, data: bytes, addr: Tuple[str, int]) -> None:
        source_ip = addr[0]
        if COMMIT_MARKER.encode() not in data:
            return  # Not a commit-complete event; skip it.

        message = parse_syslog(data)
        if message is None:
            return

        LOG.debug("Commit event from %s (%s)", message.hostname or source_ip, source_ip)
        task = asyncio.ensure_future(
            self._archiver.handle_commit(message, source_ip)
        )
        # Keep a reference so the task isn't garbage-collected mid-flight.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


# --------------------------------------------------------------------------- #
# gNMI on-change subscription
# --------------------------------------------------------------------------- #
class GnmiWatcher:
    """Subscribe to Junos commit-progress events over gNMI ``on_change``.

    One background thread per router holds a streaming gNMI subscription to
    :data:`GNMI_COMMIT_PATH` (``pygnmi`` is synchronous). When a
    ``commit complete`` message is streamed, the same :class:`CommitArchiver`
    used by the syslog path is invoked on the asyncio event loop, so the diff
    is pulled and stored exactly as for a syslog-detected commit.
    """

    def __init__(
        self,
        config: Config,
        archiver: CommitArchiver,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._config = config
        self._archiver = archiver
        self._loop = loop
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []

    def start(self) -> None:
        """Spawn one subscription thread per configured router."""
        for router in self._config.gnmi_routers:
            name = router.get("name") or router.get("hostname")
            hostname = router.get("hostname") or router.get("name")
            if not hostname:
                LOG.warning("Skipping gNMI router with no hostname: %r", router)
                continue
            thread = threading.Thread(
                target=self._run_router,
                name=f"gnmi-{name}",
                args=(name, hostname),
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ #
    def _run_router(self, name: str, hostname: str) -> None:
        """Maintain a subscription to one router, reconnecting on failure."""
        try:
            from pygnmi.client import gNMIclient
        except ImportError:  # pragma: no cover - dependency guard
            LOG.error(
                "gNMI mode requires the 'pygnmi' package (pip install pygnmi)"
            )
            return

        subscribe = {
            "subscription": [
                {"path": GNMI_COMMIT_PATH, "mode": "on_change"}
            ],
            "mode": "stream",
            "encoding": "json",
        }

        while not self._stop.is_set():
            try:
                LOG.info("gNMI: subscribing to %s (%s:%d)", name, hostname, self._config.gnmi_port)
                with gNMIclient(
                    target=(hostname, self._config.gnmi_port),
                    username=self._config.gnmi_user,
                    password=self._config.gnmi_passwd,
                    insecure=self._config.gnmi_insecure,
                    skip_verify=self._config.gnmi_skip_verify,
                    # Junos does not implement the gNMI QoS marking field and
                    # rejects the subscription with "Qos not supported"; disable
                    # the marking pygnmi otherwise sends by default.
                    no_qos_marking=True,
                ) as client:
                    for response in client.subscribe2(subscribe=subscribe):
                        if self._stop.is_set():
                            break
                        self._process_response(name, hostname, response)
            except Exception as err:  # noqa: BLE001 - keep the thread alive
                LOG.error("gNMI subscription to %s failed: %s", name, err)

            if self._stop.is_set():
                break
            self._stop.wait(self._config.gnmi_reconnect_delay)

    def _process_response(self, name: str, hostname: str, response: Any) -> None:
        """Inspect one gNMI subscribe response for a ``commit complete``."""
        if not isinstance(response, dict):
            return
        update = response.get("update")
        if not isinstance(update, dict):
            return  # sync_response / heartbeat / other control message

        timestamp = self._timestamp_from(update)
        for item in update.get("update") or []:
            path = str(item.get("path", ""))
            value = item.get("val")
            if not isinstance(value, str):
                continue
            if GNMI_COMMIT_MARKER not in value.lower():
                continue
            LOG.info(
                "gNMI: commit complete on %s (%s): %s",
                name,
                path or GNMI_COMMIT_PATH,
                value.strip(),
            )
            self._dispatch(name, hostname, timestamp)
            return

    @staticmethod
    def _timestamp_from(update: Dict[str, Any]) -> dt.datetime:
        raw = update.get("timestamp")
        if isinstance(raw, int) and raw > 0:
            try:
                return dt.datetime.fromtimestamp(raw / 1e9)
            except (OverflowError, OSError, ValueError):
                pass
        return dt.datetime.now()

    def _dispatch(self, name: str, hostname: str, timestamp: dt.datetime) -> None:
        """Hand the commit off to the archiver on the asyncio event loop."""
        message = ParsedMessage(hostname=name, timestamp=timestamp, raw="gnmi")
        # ``hostname`` is the NETCONF connect target for pulling the diff, while
        # ``name`` becomes the stored ``source`` label (mirrors the syslog path).
        asyncio.run_coroutine_threadsafe(
            self._archiver.handle_commit(message, hostname), self._loop
        )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
async def run(config: Config) -> None:
    archiver = CommitArchiver(config)
    await archiver.setup()

    loop = asyncio.get_running_loop()
    transport: Optional[asyncio.BaseTransport] = None
    gnmi_watcher: Optional[GnmiWatcher] = None

    if config.mode not in ("syslog", "gnmi", "both"):
        raise ValueError(
            f"Unknown watcher mode {config.mode!r} (expected syslog/gnmi/both)"
        )

    if config.mode in ("syslog", "both"):
        transport, _protocol = await loop.create_datagram_endpoint(
            lambda: SyslogProtocol(archiver),
            local_addr=(config.listen_host, config.listen_port),
        )
        LOG.info(
            "Listening for Junos commit syslogs on %s:%d -> MongoDB %s/%s",
            config.listen_host,
            config.listen_port,
            config.mongo_db,
            config.mongo_collection,
        )

    if config.mode in ("gnmi", "both"):
        if not config.gnmi_routers:
            LOG.warning("gNMI mode selected but no routers configured under 'gnmi.routers'")
        gnmi_watcher = GnmiWatcher(config, archiver, loop)
        gnmi_watcher.start()
        LOG.info(
            "Subscribing to gNMI on-change (%s) for %d router(s) -> MongoDB %s/%s",
            GNMI_COMMIT_PATH,
            len(config.gnmi_routers),
            config.mongo_db,
            config.mongo_collection,
        )

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows
            pass

    try:
        await stop.wait()
    finally:
        LOG.info("Shutting down ...")
        if transport is not None:
            transport.close()
        if gnmi_watcher is not None:
            gnmi_watcher.stop()
        await archiver.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "-c", "--config",
        default=os.environ.get("WATCHER_CONFIG", "config.yaml"),
        help="Path to the YAML configuration file (default: config.yaml).",
    )
    parser.add_argument(
        "-m", "--mode",
        choices=("syslog", "gnmi", "both"),
        default=None,
        help="Detection back-end (overrides watcher.mode in the config).",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging."
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    config_path = args.config if os.path.exists(args.config) else None
    if config_path is None and args.config:
        LOG.warning(
            "Config file %r not found; relying on environment/defaults", args.config
        )
    config = load_config(config_path)
    if args.mode:
        config.mode = args.mode

    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:  # pragma: no cover
        pass


if __name__ == "__main__":
    main()
