#!/usr/bin/env python3
"""OpenFlow 1.3 controller for the online-exam Mininet project.

The controller has two operating modes:

``static``
    A plain learning switch.  There is no QoS, no statistics polling, no
    abuse blocking and no VIP failover.  This is the baseline.

``sdn``
    A dynamic learning switch with:

    * event-driven exam QoS (OpenFlow meters, with an OVS queue fallback),
    * flow/port statistics collected every two seconds,
    * automatic per-host abuse blocking with a timeout,
    * health probes and VIP failover that rewrites new flows to ``srv2``.

Configuration is read from environment variables because ``osken-manager``
does not expose application-specific command-line flags.  The wrapper
``sdn/scripts/run_controller.sh`` sets the useful ones.  The complete list is
documented in ``docs/sdn-design.md``.

REST endpoints (default ``0.0.0.0:9090``)::

    POST /exam/event    exam/server event from the C server or demo script
    GET  /sdn/state     controller and failover state
    GET  /sdn/stats     flow/port/meter statistics
    POST /sdn/mode      {"mode": "static"} or {"mode": "sdn"}

The topology writes ``run/topology.json``; this controller reads it to map
datapath IDs, ports, MACs and the VIP.  Metadata can appear after the
controller starts, so it is retried by the background monitor.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import (
    CONFIG_DISPATCHER,
    MAIN_DISPATCHER,
    set_ev_cls,
)
from os_ken.lib import hub, packet
from os_ken.lib.packet import arp, ethernet, icmp, ipv4, tcp, udp
from os_ken.ofproto import ofproto_v1_3, ofproto_v1_3_parser as parser

# ---------------------------------------------------------------------------
# Defaults and OpenFlow constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUN_DIR = REPO_ROOT / "run"
DEFAULT_METADATA = DEFAULT_RUN_DIR / "topology.json"
DEFAULT_LOG_FILE = DEFAULT_RUN_DIR / "controller.ndjson"

ETH_TYPE_IP = 0x0800
ETH_TYPE_ARP = 0x0806
IPPROTO_ICMP = 1
IPPROTO_TCP = 6
IPPROTO_UDP = 17

METER_EXAM = 1
METER_BACKGROUND = 2

QUEUE_DEFAULT = 0
QUEUE_EXAM = 1
QUEUE_BACKGROUND = 2

# All controller-installed flows carry a cookie from this namespace.  Flows
# that belong to one logical group share a cookie, which makes group deletion
# a single FlowMod instead of a table walk.
COOKIE_TABLE_MISS = 0x45584D01
COOKIE_LEARNING = 0x45584D10
COOKIE_VIP_MISS = 0x45584D20
COOKIE_VIP_FLOW = 0x45584D30
COOKIE_EXAM = 0x45584D40
COOKIE_BACKGROUND = 0x45584D50
COOKIE_BLOCK = 0x45584D60

COOKIE_NAMES = {
    COOKIE_TABLE_MISS: "table_miss",
    COOKIE_LEARNING: "learning",
    COOKIE_VIP_MISS: "vip_miss",
    COOKIE_VIP_FLOW: "vip",
    COOKIE_EXAM: "exam",
    COOKIE_BACKGROUND: "background",
    COOKIE_BLOCK: "block",
}

PRIORITY_LEARNING = 10
PRIORITY_BACKGROUND = 100
PRIORITY_EXAM = 200
PRIORITY_VIP_MISS = 350
PRIORITY_VIP_FLOW = 400
PRIORITY_BLOCK = 500

FLOW_IDLE_SECONDS = 300
MAX_HTTP_BODY = 64 * 1024
KNOWN_EVENTS = frozenset(
    {
        "exam_published",
        "exam_started",
        "exam_start",
        "attempt_started",
        "attempt_submitted",
        "exam_ended",
        "exam_end",
        "exam_closed",
        "server_status",
    }
)


# ---------------------------------------------------------------------------
# Configuration and small data objects
# ---------------------------------------------------------------------------


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


@dataclass(frozen=True)
class Config:
    metadata_path: Path
    log_path: Path
    rest_host: str
    rest_port: int
    mode: str
    monitor_interval: float
    block_seconds: float
    abuse_bytes_per_sec: int
    abuse_packets_per_sec: int
    packet_in_threshold: int
    health_interval: float
    health_timeout: float
    health_failures: int
    server_port: int
    exam_rate_mbps: float
    background_rate_mbps: float
    qos_backend: str
    queue_clear_on_disable: bool

    @classmethod
    def from_env(cls) -> "Config":
        mode = _env_str("EXAM_SDN_MODE", "sdn").strip().lower()
        if mode not in ("static", "sdn"):
            raise ValueError("EXAM_SDN_MODE must be 'static' or 'sdn'")
        qos_backend = _env_str("EXAM_SDN_QOS_BACKEND", "auto").strip().lower()
        if qos_backend not in ("auto", "meter", "queue", "priority"):
            raise ValueError(
                "EXAM_SDN_QOS_BACKEND must be auto, meter, queue or priority"
            )
        rest_port = _env_int("EXAM_SDN_REST_PORT", 9090, 1)
        if rest_port > 65535:
            raise ValueError("EXAM_SDN_REST_PORT must be <= 65535")
        server_port = _env_int("EXAM_SDN_SERVER_PORT", 8080, 1)
        if server_port > 65535:
            raise ValueError("EXAM_SDN_SERVER_PORT must be <= 65535")
        return cls(
            metadata_path=Path(
                _env_str("EXAM_SDN_METADATA", str(DEFAULT_METADATA))
            ).expanduser(),
            log_path=Path(
                _env_str("EXAM_SDN_LOG", str(DEFAULT_LOG_FILE))
            ).expanduser(),
            rest_host=_env_str("EXAM_SDN_REST_HOST", "0.0.0.0"),
            rest_port=rest_port,
            mode=mode,
            monitor_interval=_env_float(
                "EXAM_SDN_MONITOR_INTERVAL", 2.0, 0.5
            ),
            block_seconds=_env_float("EXAM_SDN_BLOCK_SECONDS", 20.0, 1.0),
            abuse_bytes_per_sec=_env_int(
                "EXAM_SDN_ABUSE_BYTES_PER_SEC", 2_000_000, 1
            ),
            abuse_packets_per_sec=_env_int(
                "EXAM_SDN_ABUSE_PACKETS_PER_SEC", 5_000, 1
            ),
            packet_in_threshold=_env_int(
                "EXAM_SDN_PACKETIN_THRESHOLD", 1_500, 1
            ),
            health_interval=_env_float(
                "EXAM_SDN_HEALTH_INTERVAL", 2.0, 0.25
            ),
            health_timeout=_env_float(
                "EXAM_SDN_HEALTH_TIMEOUT", 0.5, 0.05
            ),
            health_failures=_env_int("EXAM_SDN_HEALTH_FAILURES", 2, 1),
            server_port=server_port,
            exam_rate_mbps=_env_float("EXAM_SDN_EXAM_RATE_MBPS", 8.0, 0.1),
            background_rate_mbps=_env_float(
                "EXAM_SDN_BACKGROUND_RATE_MBPS", 2.0, 0.1
            ),
            qos_backend=qos_backend,
            queue_clear_on_disable=_env_str(
                "EXAM_SDN_QUEUE_CLEAR", "1"
            ).lower()
            not in ("0", "false", "no"),
        )


@dataclass
class HostEntry:
    name: str
    ip: str
    mac: str
    switch: str
    side: str
    role: str
    port: Optional[int]


@dataclass
class SwitchEntry:
    name: str
    dpid: int
    host_ports: dict[str, int]
    ports: dict[str, int]
    bottleneck_port: Optional[int]
    root_port: Optional[int]


@dataclass
class BlockEntry:
    host: str
    ip: str
    mac: str
    switch: str
    port: Optional[int]
    reason: str
    started_at: float
    expires_at: float
    cookie: int
    rx_bps: float
    rx_pps: float


@dataclass
class PortSample:
    at: float
    rx_bytes: int
    rx_packets: int
    tx_bytes: int
    tx_packets: int
    rx_dropped: int = 0
    tx_dropped: int = 0


@dataclass
class MeterSample:
    meter_id: int
    flow_count: int
    packet_count: int
    byte_count: int
    at: float


class JsonlLog:
    """Thread-safe append-only JSON-lines decision log."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._closed = False
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(path, "a", encoding="utf-8")

    def write(self, event_name: str, **fields: Any) -> None:
        record: dict[str, Any] = {
            "ts": round(time.time(), 3),
            "event": event_name,
        }
        record.update(fields)
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=True)
        with self._lock:
            if self._closed:
                return
            self._handle.write(line + "\n")
            self._handle.flush()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._handle.close()
            except OSError:
                pass


def _normalise_server_key(value: Any) -> Optional[str]:
    """Reduce ``10.0.0.1:8080`` to ``10.0.0.1`` when possible."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("["):
        end = text.find("]")
        if end > 1:
            return text[1:end]
    if text.count(":") == 1:
        host, port = text.rsplit(":", 1)
        if port.isdigit() and host:
            return host
    return text


def _safe_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return round(number, 6)


def _wall_iso(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


# ---------------------------------------------------------------------------
# REST HTTP server
# ---------------------------------------------------------------------------


class _RestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ExamSDNController/1.0"

    def setup(self) -> None:
        super().setup()
        try:
            self.connection.settimeout(5.0)
        except OSError:
            pass

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        self._dispatch("POST")

    def do_HEAD(self) -> None:  # noqa: N802 - http.server API
        self._dispatch("HEAD")

    def do_PUT(self) -> None:  # noqa: N802 - http.server API
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802 - http.server API
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        controller: "ExamController" = self.server.controller  # type: ignore[attr-defined]
        path = urlsplit(self.path).path or "/"
        try:
            body = self._read_body()
        except ValueError as exc:
            self._send_json(400, {"error": "bad_request", "detail": str(exc)})
            return
        try:
            status, payload, headers = controller.handle_rest(
                method, path, body
            )
        except Exception as exc:  # noqa: BLE001 - API must stay alive
            controller.logger.exception("REST handler failed")
            controller.decision(
                "rest_error",
                method=method,
                path=path,
                error=f"{type(exc).__name__}: {exc}",
            )
            status, payload, headers = (
                500,
                {"error": "internal_error"},
                {},
            )
        self._send_json(status, payload, headers)

    def _read_body(self) -> bytes:
        transfer_encoding = self.headers.get("Transfer-Encoding")
        if transfer_encoding and transfer_encoding.lower() != "identity":
            raise ValueError("chunked bodies are not supported")
        raw_length = self.headers.get("Content-Length", "0")
        if not raw_length.isascii() or not raw_length.isdigit():
            raise ValueError("invalid Content-Length")
        length = int(raw_length)
        if length > MAX_HTTP_BODY:
            raise ValueError("request body too large")
        if length == 0:
            return b""
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError("truncated request body")
        return body

    def _send_json(
        self,
        status: int,
        payload: dict[str, Any],
        headers: Optional[dict[str, str]] = None,
    ) -> None:
        body = (
            json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
            + "\n"
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        self.close_connection = True

    def log_message(self, _fmt: str, *args: Any) -> None:
        # Keep the controller's stdout clean; decisions go to the JSONL file.
        return


class _ControllerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(
        self,
        address: tuple[str, int],
        controller: "ExamController",
    ) -> None:
        self.controller = controller
        super().__init__(address, _RestHandler)


class RestServer:
    def __init__(self, controller: "ExamController", host: str, port: int) -> None:
        self._controller = controller
        self._host = host
        self._port = port
        self._httpd: Optional[_ControllerHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.bound_port = port

    def start(self) -> None:
        self._httpd = _ControllerHTTPServer(
            (self._host, self._port), self._controller
        )
        self.bound_port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.25},
            name="exam-sdn-rest",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        httpd = self._httpd
        if httpd is None:
            return
        try:
            httpd.shutdown()
        except Exception:  # noqa: BLE001 - shutdown races are harmless
            pass
        try:
            httpd.server_close()
        except OSError:
            pass
        thread = self._thread
        if thread is not None:
            thread.join(2.0)
        self._httpd = None
        self._thread = None


# ---------------------------------------------------------------------------
# Controller application
# ---------------------------------------------------------------------------


class ExamController(app_manager.OSKenApp):
    """OpenFlow 1.3 exam SDN controller."""

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.cfg = Config.from_env()
        self.lock = threading.RLock()
        self.decision_log = JsonlLog(self.cfg.log_path)

        self.datapaths: dict[int, Any] = {}
        self.switch_entries: dict[int, SwitchEntry] = {}
        self.metadata: Optional[dict[str, Any]] = None
        self.metadata_mtime: Optional[float] = None
        self.metadata_error: Optional[str] = None
        self.metadata_loaded_at: Optional[float] = None

        self.hosts_by_ip: dict[str, HostEntry] = {}
        self.hosts_by_mac: dict[str, HostEntry] = {}
        self.hosts_by_switch: dict[str, list[HostEntry]] = {}
        self.servers_by_ip: dict[str, HostEntry] = {}
        self.server_order: list[str] = []
        self.server_ips: set[str] = set()
        self.switches_by_name: dict[str, SwitchEntry] = {}
        self.name_by_dpid: dict[int, str] = {}
        self.vip_ip: Optional[str] = None
        self.vip_mac: Optional[str] = None

        self.mode = self.cfg.mode
        self.mode_changed_at = time.time()
        self.started_at = time.time()

        self.exam_active = False
        self.exam_lifecycle_active = False
        self.exam_id: Optional[int] = None
        self.exam_active_changed_at: Optional[float] = None
        self.event_count = 0
        self.event_counts: Counter[str] = Counter()
        self.last_event: Optional[dict[str, Any]] = None
        self.last_event_at: Optional[float] = None
        self.server_attempts: dict[str, int] = {}
        self.server_last_event: dict[str, float] = {}
        self.server_health: dict[str, dict[str, Any]] = {}

        self.vip_backend_ip: Optional[str] = None
        self.vip_failovers = 0
        self.vip_last_failover_at: Optional[float] = None
        self.vip_flow_count = 0

        self.qos_active = False
        self.qos_backend_by_dpid: dict[int, str] = {}
        self.meter_support: dict[int, Optional[bool]] = {}
        self.meter_requested: set[int] = set()
        self.meters_installed: set[int] = set()
        self.queue_fallback_active = False
        self.queue_fallback_attempted = False
        self.queue_ports: list[str] = []

        self.port_samples: dict[tuple[int, int], PortSample] = {}
        self.host_metrics: dict[str, dict[str, Any]] = {}
        self.flow_stats_by_dpid: dict[int, dict[str, Any]] = {}
        self.meter_samples: dict[tuple[int, int], MeterSample] = {}
        self.last_flow_stats_at: Optional[float] = None
        self.last_port_stats_at: Optional[float] = None
        self.last_meter_stats_at: Optional[float] = None
        self.packet_in_count = 0
        self.packet_in_by_host: dict[str, deque[float]] = {}

        self.blocked: dict[str, BlockEntry] = {}
        self.block_sequence = 0

        self._stop_event = threading.Event()
        self._background_threads: list[threading.Thread] = []
        self._rest = RestServer(self, self.cfg.rest_host, self.cfg.rest_port)
        self._closed = False

        self._reload_metadata(reason="startup")
        self._initialise_health_state()
        self.decision(
            "controller_init",
            mode=self.mode,
            metadata=str(self.cfg.metadata_path),
            metadata_loaded=self.metadata is not None,
            rest_port=self.cfg.rest_port,
            monitor_interval_s=self.cfg.monitor_interval,
            block_seconds=self.cfg.block_seconds,
            health_interval_s=self.cfg.health_interval,
            health_failures=self.cfg.health_failures,
            qos_backend=self.cfg.qos_backend,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        super().start()
        try:
            self._rest.start()
            self.logger.info(
                "REST API on %s:%d", self.cfg.rest_host, self._rest.bound_port
            )
            self.decision(
                "rest_listening",
                host=self.cfg.rest_host,
                port=self._rest.bound_port,
            )
        except OSError as exc:
            self.logger.error("REST server could not start: %s", exc)
            self.decision("rest_error", error=str(exc))

        self._spawn_background("exam-sdn-monitor", self._monitor_loop)
        self._spawn_background("exam-sdn-health", self._health_loop)

    def _spawn_background(self, name: str, target: Callable[[], None]) -> None:
        thread = threading.Thread(
            target=target, name=name, daemon=True
        )
        thread.start()
        self._background_threads.append(thread)

    def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop_event.set()
        self._rest.stop()
        for thread in self._background_threads:
            thread.join(2.0)
        self._background_threads.clear()
        self.decision("controller_stop")
        self.decision_log.close()
        try:
            super().stop()
        except Exception:  # noqa: BLE001 - manager shutdown best effort
            pass

    def close(self) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # Metadata and naming
    # ------------------------------------------------------------------

    def _server_health_default(self) -> dict[str, Any]:
        return {
            "healthy": None,
            "consecutive_failures": 0,
            "last_probe_at": None,
            "last_ok_at": None,
            "last_error": None,
            "latency_ms": None,
            "probe_attempts": 0,
            "probe_failures": 0,
        }

    def _initialise_health_state(self) -> None:
        for ip in self.server_ips:
            self.server_health.setdefault(ip, self._server_health_default())

    def _reload_metadata(self, reason: str = "periodic") -> bool:
        path = self.cfg.metadata_path
        try:
            stat = path.stat()
        except OSError as exc:
            if self.metadata_error != str(exc):
                self.metadata_error = str(exc)
                self.logger.warning("topology metadata unavailable: %s", exc)
            return False
        if (
            self.metadata is not None
            and self.metadata_mtime is not None
            and stat.st_mtime == self.metadata_mtime
        ):
            return True
        try:
            with open(path, encoding="utf-8") as handle:
                raw = json.load(handle)
            if not isinstance(raw, dict):
                raise ValueError("topology metadata must be a JSON object")
            self._apply_metadata(raw)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self.metadata_error = str(exc)
            self.logger.warning("topology metadata invalid: %s", exc)
            self.decision("metadata_error", error=str(exc), path=str(path))
            return False
        self.metadata = raw
        self.metadata_mtime = stat.st_mtime
        self.metadata_error = None
        self.metadata_loaded_at = time.time()
        self._initialise_health_state()
        self.logger.info("loaded topology metadata from %s", path)
        self.decision(
            "metadata_loaded",
            reason=reason,
            path=str(path),
            switches=sorted(self.switches_by_name),
            servers=sorted(self.server_ips),
            vip=self.vip_ip,
            students=raw.get("students"),
        )
        return True

    def _apply_metadata(self, raw: dict[str, Any]) -> None:
        switches_raw = raw.get("switches")
        if not isinstance(switches_raw, dict):
            raise ValueError("missing switches object")
        new_switches: dict[str, SwitchEntry] = {}
        new_name_by_dpid: dict[int, str] = {}
        for name, item in switches_raw.items():
            if not isinstance(item, dict):
                continue
            dpid = item.get("dpid_int")
            if dpid is None and item.get("dpid") is not None:
                dpid = int(str(item["dpid"]), 16)
            if dpid is None:
                continue
            host_ports = {
                str(key): int(value)
                for key, value in (item.get("host_ports") or {}).items()
                if value is not None
            }
            ports = {
                str(key): int(value)
                for key, value in (item.get("ports") or {}).items()
                if value is not None
            }
            entry = SwitchEntry(
                name=str(name),
                dpid=int(dpid),
                host_ports=host_ports,
                ports=ports,
                bottleneck_port=(
                    int(item["bottleneck_port"])
                    if item.get("bottleneck_port") is not None
                    else None
                ),
                root_port=(
                    int(item["root_port"])
                    if item.get("root_port") is not None
                    else None
                ),
            )
            new_switches[entry.name] = entry
            new_name_by_dpid[entry.dpid] = entry.name

        hosts_by_ip: dict[str, HostEntry] = {}
        hosts_by_mac: dict[str, HostEntry] = {}
        hosts_by_switch: dict[str, list[HostEntry]] = {}
        servers: dict[str, HostEntry] = {}

        def add_host(item: dict[str, Any], default_side: str = "") -> None:
            ip = item.get("ip")
            mac = item.get("mac")
            name = item.get("name")
            switch = item.get("switch")
            if not (isinstance(ip, str) and isinstance(mac, str) and name):
                return
            if switch not in new_switches:
                return
            host_entry = HostEntry(
                name=str(name),
                ip=ip,
                mac=str(mac).lower(),
                switch=str(switch),
                side=str(item.get("side", default_side)),
                role=str(item.get("role", "host")),
                port=(
                    int(item["port"])
                    if item.get("port") is not None
                    else None
                ),
            )
            hosts_by_ip[ip] = host_entry
            hosts_by_mac[host_entry.mac] = host_entry
            hosts_by_switch.setdefault(host_entry.switch, []).append(host_entry)

        servers_raw = raw.get("servers")
        if isinstance(servers_raw, dict):
            for item in servers_raw.values():
                if isinstance(item, dict):
                    add_host(item, "server")
        clients_raw = raw.get("clients")
        if isinstance(clients_raw, dict):
            for item in clients_raw.values():
                if isinstance(item, dict):
                    add_host(item, "student")
        root_raw = raw.get("root")
        if isinstance(root_raw, dict) and root_raw.get("ip"):
            root_item = dict(root_raw)
            root_item.setdefault("name", "root")
            root_item.setdefault("role", "root")
            root_item.setdefault("side", "server")
            add_host(root_item, "server")

        server_entries = {
            ip: host for ip, host in hosts_by_ip.items()
            if host.role == "exam_server" or host.side == "server"
        }
        # Keep only true exam servers in the failover preference list.
        server_entries = {
            ip: host
            for ip, host in server_entries.items()
            if host.role == "exam_server"
        }
        server_order = sorted(
            server_entries.values(), key=lambda host: host.name
        )
        server_order_ips = [host.ip for host in server_order]

        vip = raw.get("vip")
        vip_mac = raw.get("vip_mac")
        if vip is not None and not isinstance(vip, str):
            raise ValueError("vip must be a string")

        self.switches_by_name = new_switches
        self.name_by_dpid = new_name_by_dpid
        self.hosts_by_ip = hosts_by_ip
        self.hosts_by_mac = hosts_by_mac
        self.hosts_by_switch = hosts_by_switch
        self.servers_by_ip = server_entries
        self.server_ips = set(server_entries)
        self.server_order = server_order_ips
        self.vip_ip = vip
        self.vip_mac = str(vip_mac).lower() if vip_mac else None
        if self.vip_backend_ip not in self.server_ips:
            self.vip_backend_ip = (
                server_order_ips[0] if server_order_ips else None
            )

    def _switch_entry(self, dpid: int) -> Optional[SwitchEntry]:
        return self.switch_entries.get(dpid) or self.switches_by_name.get(
            self.name_by_dpid.get(dpid, "")
        )

    def _switch_name(self, dpid: int) -> str:
        entry = self._switch_entry(dpid)
        if entry is not None:
            return entry.name
        return f"dpid-{dpid:016x}"

    def _host_for_ip(self, ip: Optional[str]) -> Optional[HostEntry]:
        if ip is None:
            return None
        return self.hosts_by_ip.get(ip)

    def _host_for_mac(self, mac: Optional[str]) -> Optional[HostEntry]:
        if mac is None:
            return None
        return self.hosts_by_mac.get(str(mac).lower())

    def _server_name(self, ip: Optional[str]) -> Optional[str]:
        host = self._host_for_ip(ip)
        return host.name if host is not None else None

    def _port_for_route(
        self, current: SwitchEntry, target: Optional[HostEntry]
    ) -> Optional[int]:
        if target is None:
            return None
        if target.switch == current.name and target.port is not None:
            return target.port
        return current.bottleneck_port

    # ------------------------------------------------------------------
    # Decision logging / REST dispatch
    # ------------------------------------------------------------------

    def decision(self, event_name: str, **fields: Any) -> None:
        try:
            self.decision_log.write(event_name, **fields)
        except Exception:  # noqa: BLE001 - logging must not kill forwarding
            self.logger.exception("could not write decision log")

    def handle_rest(
        self, method: str, path: str, body: bytes
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        if path == "/health" and method == "GET":
            return 200, {
                "status": "ok",
                "service": "exam-sdn-controller",
                "mode": self.mode,
                "uptime_s": round(time.time() - self.started_at, 3),
            }, {}
        if path == "/sdn/state":
            if method != "GET":
                return 405, {"error": "method_not_allowed"}, {"Allow": "GET"}
            return 200, self.state_snapshot(), {}
        if path == "/sdn/stats":
            if method != "GET":
                return 405, {"error": "method_not_allowed"}, {"Allow": "GET"}
            return 200, self.stats_snapshot(), {}
        if path == "/sdn/mode":
            if method != "POST":
                return 405, {"error": "method_not_allowed"}, {"Allow": "POST"}
            payload = self._parse_json_body(body)
            if payload is None:
                return 400, {"error": "invalid_json"}, {}
            mode = payload.get("mode")
            if not isinstance(mode, str) or mode.strip().lower() not in (
                "static",
                "sdn",
            ):
                return 400, {
                    "error": "invalid_mode",
                    "detail": "mode must be 'static' or 'sdn'",
                }, {}
            mode = mode.strip().lower()
            with self.lock:
                changed = self._set_mode_locked(mode, source="rest")
                return 200, {
                    "ok": True,
                    "mode": self.mode,
                    "changed": changed,
                    "exam_active": self.exam_active,
                }, {}
        if path == "/exam/event":
            if method != "POST":
                return 405, {"error": "method_not_allowed"}, {"Allow": "POST"}
            payload = self._parse_json_body(body)
            if payload is None:
                return 400, {"error": "invalid_json"}, {}
            accepted, error = self._validate_event(payload)
            if not accepted:
                return 400, {"error": "invalid_event", "detail": error}, {}
            with self.lock:
                self._handle_event_locked(payload)
                self.decision(
                    "api_event_accepted",
                    received_event=payload.get("event"),
                    server=payload.get("server"),
                    exam_id=payload.get("exam_id"),
                    active_attempts=payload.get("active_attempts"),
                    exam_active=self.exam_active,
                    mode=self.mode,
                )
                return 200, {
                    "ok": True,
                    "event": payload.get("event"),
                    "mode": self.mode,
                    "exam_active": self.exam_active,
                    "exam_id": self.exam_id,
                    "active_attempts": self._total_attempts_locked(),
                }, {}
        if path.startswith("/sdn/"):
            return 404, {"error": "not_found"}, {}
        return 404, {"error": "not_found"}, {}

    @staticmethod
    def _parse_json_body(body: bytes) -> Optional[dict[str, Any]]:
        if not body:
            return None
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    @staticmethod
    def _validate_event(payload: dict[str, Any]) -> tuple[bool, Optional[str]]:
        event = payload.get("event")
        if not isinstance(event, str) or not event.strip():
            return False, "event must be a non-empty string"
        if event not in KNOWN_EVENTS:
            return False, f"unknown event: {event}"
        server = payload.get("server")
        if server is not None and not isinstance(server, str):
            return False, "server must be a string or null"
        exam_id = payload.get("exam_id")
        if exam_id is not None and (
            isinstance(exam_id, bool) or not isinstance(exam_id, int)
        ):
            return False, "exam_id must be an integer or null"
        for name in ("active_attempts", "active_connections"):
            if name in payload and payload[name] is not None:
                value = payload[name]
                if isinstance(value, bool) or not isinstance(value, int):
                    return False, f"{name} must be a non-negative integer"
                if value < 0:
                    return False, f"{name} must be a non-negative integer"
        if "exam_active" in payload and payload["exam_active"] is not None:
            if not isinstance(payload["exam_active"], bool):
                return False, "exam_active must be a boolean"
        if "ts" in payload and payload["ts"] is not None:
            if _safe_number(payload["ts"]) is None:
                return False, "ts must be a finite number"
        return True, None

    # ------------------------------------------------------------------
    # Mode and exam event handling
    # ------------------------------------------------------------------

    def _set_mode_locked(self, mode: str, source: str = "api") -> bool:
        if mode == self.mode:
            return False
        old = self.mode
        self.mode = mode
        self.mode_changed_at = time.time()
        # Every dynamic rule is mode-specific.  Reset once, then rebuild the
        # static/SDN baseline.  Traffic re-learns on the next packet.
        self._clear_blocks_locked(reason=f"mode:{mode}", delete_flows=False)
        if old == "sdn":
            self._disable_qos_locked(reset_flows=False)
        self._reset_all_flows_locked(reason=f"mode:{mode}")
        if mode == "sdn":
            self._reload_metadata(reason="mode")
            if self.exam_active:
                self._enable_qos_locked()
            self._request_meter_features_locked()
        else:
            self._queue_clear_if_needed()
            self.port_samples.clear()
            self.host_metrics.clear()
            self.flow_stats_by_dpid.clear()
            self.meter_samples.clear()
        self.decision(
            "mode_changed",
            old=old,
            new=mode,
            source=source,
            exam_active=self.exam_active,
            qos_active=self.qos_active,
        )
        self.logger.info("mode changed from %s to %s", old, mode)
        return True

    def _handle_event_locked(self, payload: dict[str, Any]) -> None:
        event = str(payload["event"])
        now = time.time()
        self.event_count += 1
        self.event_counts[event] += 1
        self.last_event = dict(payload)
        self.last_event_at = now

        server_key = _normalise_server_key(payload.get("server"))
        exam_id = payload.get("exam_id")
        attempts = payload.get("active_attempts")

        if server_key is not None:
            self.server_last_event[server_key] = now
            if exam_id is not None and isinstance(exam_id, int):
                self.exam_id = exam_id
        if (
            server_key is not None
            and isinstance(attempts, int)
            and not isinstance(attempts, bool)
        ):
            self.server_attempts[server_key] = attempts

        if event in ("exam_published",):
            if isinstance(exam_id, int):
                self.exam_id = exam_id
            # An exam can be published before anyone starts.  Keep the
            # lifecycle flag off until an attempt or an explicit exam_start.
        elif event in ("exam_started", "exam_start"):
            if isinstance(exam_id, int):
                self.exam_id = exam_id
            self.exam_lifecycle_active = True
        elif event in ("attempt_started",):
            if isinstance(exam_id, int):
                self.exam_id = exam_id
            # A positive counter is authoritative and implies an active exam.
        elif event in ("attempt_submitted",):
            if isinstance(exam_id, int):
                self.exam_id = exam_id
        elif event in ("exam_ended", "exam_end", "exam_closed"):
            if isinstance(exam_id, int):
                self.exam_id = exam_id
            self.exam_lifecycle_active = False
            self.server_attempts.clear()
        elif event == "server_status":
            if isinstance(exam_id, int):
                self.exam_id = exam_id

        if "exam_active" in payload and isinstance(payload["exam_active"], bool):
            self.exam_lifecycle_active = bool(payload["exam_active"])

        self._refresh_exam_active_locked(event=event)
        self._log_event_decision(event, payload, server_key)

    def _log_event_decision(
        self,
        event: str,
        payload: dict[str, Any],
        server_key: Optional[str],
    ) -> None:
        if event == "server_status":
            # Heartbeats are frequent; log only when they changed something
            # interesting.  The state and stats endpoints stay live.
            return
        self.decision(
            "exam_event",
            received_event=event,
            server=server_key,
            exam_id=payload.get("exam_id"),
            active_attempts=payload.get("active_attempts"),
            exam_active=self.exam_active,
            lifecycle=bool(self.exam_lifecycle_active),
        )

    def _total_attempts_locked(self) -> int:
        return sum(max(0, int(value)) for value in self.server_attempts.values())

    def _refresh_exam_active_locked(self, event: str = "update") -> None:
        desired = bool(self.exam_lifecycle_active or self._total_attempts_locked())
        if desired == self.exam_active:
            return
        self.exam_active = desired
        self.exam_active_changed_at = time.time()
        self.decision(
            "exam_state",
            active=desired,
            received_event=event,
            exam_id=self.exam_id,
            active_attempts=self._total_attempts_locked(),
            lifecycle=bool(self.exam_lifecycle_active),
            mode=self.mode,
        )
        self.logger.info(
            "exam is now %s (attempts=%d)",
            "active" if desired else "inactive",
            self._total_attempts_locked(),
        )
        if self.mode != "sdn":
            return
        if desired:
            self._enable_qos_locked()
        else:
            self._disable_qos_locked()

    def _server_active_attempts_locked(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for ip in self.server_ips:
            value = self.server_attempts.get(ip, 0)
            result[self._server_name(ip) or ip] = value
        for key, value in self.server_attempts.items():
            if key not in self.server_ips:
                result[key] = value
        return result

    # ------------------------------------------------------------------
    # OpenFlow helpers
    # ------------------------------------------------------------------

    def _send(self, datapath: Any, message: Any) -> bool:
        try:
            datapath.send_msg(message)
            return True
        except Exception as exc:  # noqa: BLE001 - switch may disappear
            self.logger.warning(
                "send to %s failed: %s", self._switch_name(datapath.id), exc
            )
            return False

    def _install_table_miss(self, datapath: Any) -> None:
        ofp = datapath.ofproto
        actions = [
            parser.OFPActionOutput(
                ofp.OFPP_CONTROLLER, max_len=ofp.OFPCML_NO_BUFFER
            )
        ]
        self._add_flow(
            datapath,
            priority=0,
            match=parser.OFPMatch(),
            actions=actions,
            cookie=COOKIE_TABLE_MISS,
        )

    def _install_vip_miss(self, datapath: Any) -> None:
        if not self.vip_ip:
            return
        ofp = datapath.ofproto
        actions = [
            parser.OFPActionOutput(
                ofp.OFPP_CONTROLLER, max_len=ofp.OFPCML_NO_BUFFER
            )
        ]
        match = parser.OFPMatch(
            eth_type=ETH_TYPE_IP, ipv4_dst=self.vip_ip
        )
        self._add_flow(
            datapath,
            priority=PRIORITY_VIP_MISS,
            match=match,
            actions=actions,
            cookie=COOKIE_VIP_MISS,
            idle_timeout=0,
        )

    def _add_flow(
        self,
        datapath: Any,
        *,
        priority: int,
        match: Any,
        actions: list[Any],
        cookie: int,
        idle_timeout: int = FLOW_IDLE_SECONDS,
        hard_timeout: int = 0,
        meter_id: Optional[int] = None,
        queue_id: Optional[int] = None,
    ) -> bool:
        ofp = datapath.ofproto
        parser_cls = datapath.ofproto_parser
        actual_actions = list(actions)
        if queue_id is not None:
            actual_actions.insert(0, parser.OFPActionSetQueue(queue_id))
        instructions: list[Any] = []
        if meter_id is not None:
            instructions.append(parser_cls.OFPInstructionMeter(meter_id))
        if actual_actions:
            instructions.append(
                parser_cls.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, actual_actions
                )
            )
        flow_mod = parser_cls.OFPFlowMod(
            datapath,
            cookie=cookie,
            command=ofp.OFPFC_ADD,
            idle_timeout=idle_timeout,
            hard_timeout=hard_timeout,
            priority=priority,
            match=match,
            instructions=instructions,
        )
        return self._send(datapath, flow_mod)

    def _delete_all_flows(self, datapath: Any) -> None:
        ofp = datapath.ofproto
        flow_mod = datapath.ofproto_parser.OFPFlowMod(
            datapath,
            cookie=0,
            cookie_mask=0,
            table_id=ofp.OFPTT_ALL,
            command=ofp.OFPFC_DELETE,
            out_port=ofp.OFPP_ANY,
            out_group=ofp.OFPG_ANY,
            match=datapath.ofproto_parser.OFPMatch(),
        )
        self._send(datapath, flow_mod)

    def _delete_cookie(self, datapath: Any, cookie: int) -> None:
        ofp = datapath.ofproto
        flow_mod = datapath.ofproto_parser.OFPFlowMod(
            datapath,
            cookie=cookie,
            cookie_mask=0xFFFFFFFFFFFFFFFF,
            table_id=ofp.OFPTT_ALL,
            command=ofp.OFPFC_DELETE,
            out_port=ofp.OFPP_ANY,
            out_group=ofp.OFPG_ANY,
            match=datapath.ofproto_parser.OFPMatch(),
        )
        self._send(datapath, flow_mod)

    def _packet_out(
        self,
        datapath: Any,
        in_port: int,
        actions: list[Any],
        data: bytes,
        buffer_id: int,
    ) -> None:
        ofp = datapath.ofproto
        kwargs: dict[str, Any] = {
            "buffer_id": buffer_id,
            "in_port": in_port,
            "actions": actions,
        }
        if buffer_id == ofp.OFP_NO_BUFFER or not data:
            kwargs["buffer_id"] = ofp.OFP_NO_BUFFER
            kwargs["data"] = data
        out = datapath.ofproto_parser.OFPPacketOut(datapath, **kwargs)
        self._send(datapath, out)

    def _flood(self, datapath: Any, in_port: int, data: bytes, buffer_id: int) -> None:
        actions = [parser.OFPActionOutput(datapath.ofproto.OFPP_FLOOD)]
        self._packet_out(datapath, in_port, actions, data, buffer_id)

    def _reset_all_flows_locked(self, reason: str) -> None:
        for dpid, datapath in list(self.datapaths.items()):
            self._delete_all_flows(datapath)
            self._install_table_miss(datapath)
            if self.mode == "sdn":
                self._install_vip_miss(datapath)
            # Meter objects live independently of the flow table.  Do not
            # discard their readiness here; flows are simply reclassified
            # against the already-configured meters.
            self.flow_stats_by_dpid[dpid] = {
                "flow_count": 0,
                "packet_count": 0,
                "byte_count": 0,
                "categories": {},
                "updated_at": time.time(),
            }
            for entry in self.blocked.values():
                if entry.switch == self._switch_name(dpid):
                    self._install_block_flow_locked(entry)
        self.vip_flow_count = 0
        self.decision("flows_reset", reason=reason, mode=self.mode)

    # ------------------------------------------------------------------
    # QoS: meters, queues and fallback
    # ------------------------------------------------------------------

    def _request_meter_features_locked(self) -> None:
        if self.mode != "sdn" or self.cfg.qos_backend not in ("auto", "meter"):
            return
        for dpid, datapath in list(self.datapaths.items()):
            if dpid in self.meter_requested:
                continue
            self.meter_requested.add(dpid)
            try:
                request = datapath.ofproto_parser.OFPMeterFeaturesStatsRequest(
                    datapath
                )
                self._send(datapath, request)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("meter features request failed: %s", exc)
                self.meter_support[dpid] = False

    def _enable_qos_locked(self) -> None:
        if self.mode != "sdn" or self.qos_active:
            return
        self.qos_active = True
        self.decision(
            "qos_enabled",
            exam_rate_mbps=self.cfg.exam_rate_mbps,
            background_rate_mbps=self.cfg.background_rate_mbps,
            requested_backend=self.cfg.qos_backend,
        )
        if self.cfg.qos_backend == "priority":
            for dpid in self.datapaths:
                self.qos_backend_by_dpid[dpid] = "priority"
            self._reset_all_flows_locked(reason="qos:priority")
            return
        if self.cfg.qos_backend == "queue":
            if self._try_queue_fallback_locked("forced"):
                self._reset_all_flows_locked(reason="qos:queue")
            else:
                for dpid in self.datapaths:
                    self.qos_backend_by_dpid[dpid] = "priority"
                self._reset_all_flows_locked(reason="qos:priority-fallback")
            return
        # A previous SDN/QoS cycle deleted the meter objects.  Forget the
        # old feature-cache so the switches are queried again.
        for dpid in self.datapaths:
            self.meter_support[dpid] = None
            self.meter_requested.discard(dpid)
        self._request_meter_features_locked()
        self._install_meters_locked()
        self._reset_all_flows_locked(reason="qos:meter-or-pending")

    def _disable_qos_locked(self, reset_flows: bool = True) -> None:
        if not self.qos_active and not self.queue_fallback_active:
            if reset_flows:
                self._reset_all_flows_locked(reason="qos:off")
            return
        was_active = self.qos_active
        self.qos_active = False
        for dpid, datapath in list(self.datapaths.items()):
            if dpid in self.meters_installed:
                self._delete_meters(datapath)
                self.meters_installed.discard(dpid)
            self.qos_backend_by_dpid[dpid] = "priority"
        self._queue_clear_if_needed()
        if reset_flows:
            self._reset_all_flows_locked(reason="qos:off")
        self.decision(
            "qos_disabled",
            was_active=was_active,
            queue_fallback_was_active=self.queue_fallback_active,
        )
        self.queue_fallback_active = False
        self.queue_fallback_attempted = False

    def _install_meters_locked(self) -> None:
        if not self.qos_active:
            return
        for dpid, datapath in list(self.datapaths.items()):
            if self.meter_support.get(dpid) is False:
                continue
            if dpid in self.meters_installed:
                continue
            if self.meter_support.get(dpid) is not True:
                self._request_meter_features_locked()
                continue
            self._install_meters_on(datapath)

    def _install_meters_on(self, datapath: Any) -> None:
        if datapath.id in self.meters_installed:
            return
        ofp = datapath.ofproto
        parser_cls = datapath.ofproto_parser
        # Delete first: a switch may still hold meters from a controller
        # restart while the datapath itself is unchanged.
        try:
            for meter_id in (METER_EXAM, METER_BACKGROUND):
                self._send(
                    datapath,
                    parser_cls.OFPMeterMod(
                        datapath,
                        command=ofp.OFPMC_DELETE,
                        flags=0,
                        meter_id=meter_id,
                    ),
                )
            rates = {
                METER_EXAM: self.cfg.exam_rate_mbps,
                METER_BACKGROUND: self.cfg.background_rate_mbps,
            }
            for meter_id, mbps in rates.items():
                rate_kbps = max(1, int(mbps * 1000))
                band = parser_cls.OFPMeterBandDrop(
                    rate=rate_kbps,
                    burst_size=max(100, int(rate_kbps * 0.10)),
                )
                mod = parser_cls.OFPMeterMod(
                    datapath,
                    command=ofp.OFPMC_ADD,
                    flags=ofp.OFPMF_KBPS | ofp.OFPMF_BURST | ofp.OFPMF_STATS,
                    meter_id=meter_id,
                    bands=[band],
                )
                if not self._send(datapath, mod):
                    raise RuntimeError("OFPMeterMod send failed")
            self.meters_installed.add(datapath.id)
            self.meter_support[datapath.id] = True
            self.qos_backend_by_dpid[datapath.id] = "meter"
            self.decision(
                "meters_installed",
                switch=self._switch_name(datapath.id),
                exam_rate_kbps=rates[METER_EXAM] * 1000,
                background_rate_kbps=rates[METER_BACKGROUND] * 1000,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("meter setup failed on %s: %s", self._switch_name(datapath.id), exc)
            self.meter_support[datapath.id] = False
            self.qos_backend_by_dpid[datapath.id] = "priority"
            self._try_queue_fallback_locked("meter-error")

    def _delete_meters(self, datapath: Any) -> None:
        ofp = datapath.ofproto
        parser_cls = datapath.ofproto_parser
        for meter_id in (METER_EXAM, METER_BACKGROUND):
            try:
                self._send(
                    datapath,
                    parser_cls.OFPMeterMod(
                        datapath,
                        command=ofp.OFPMC_DELETE,
                        flags=0,
                        meter_id=meter_id,
                    ),
                )
            except Exception:  # noqa: BLE001
                pass

    def _try_queue_fallback_locked(self, reason: str) -> bool:
        if self.queue_fallback_active:
            return True
        if self.cfg.qos_backend == "priority":
            return False
        if self.queue_fallback_attempted and not self.queue_fallback_active:
            return False
        self.queue_fallback_attempted = True
        if os.geteuid() != 0:
            self.decision(
                "queue_fallback_failed",
                reason="not_root",
                trigger=reason,
            )
            return False
        if not self.metadata:
            return False
        bottleneck = self.metadata.get("bottleneck") or {}
        ports = [
            bottleneck.get("student_iface"),
            bottleneck.get("server_iface"),
        ]
        ports = [str(port) for port in ports if port]
        if not ports:
            self.decision(
                "queue_fallback_failed", reason="missing_ports", trigger=reason
            )
            return False
        configured: list[str] = []
        exam_bps = int(self.cfg.exam_rate_mbps * 1_000_000)
        bg_bps = int(self.cfg.background_rate_mbps * 1_000_000)
        bottleneck_bps = int(
            self.metadata.get("bottleneck", {}).get("bw_mbps", 10.0)
            * 1_000_000
        )
        for port in ports:
            command = [
                "ovs-vsctl",
                "--",
                "set",
                "Port",
                port,
                "qos=@q",
                "--",
                "--id=@q",
                "create",
                "QoS",
                "type=linux-htb",
                f"other-config:max-rate={bottleneck_bps}",
                "queues:0=@q0",
                "queues:1=@q1",
                "queues:2=@q2",
                "--",
                "--id=@q0",
                "create",
                "Queue",
                f"other-config:max-rate={bottleneck_bps}",
                "--",
                "--id=@q1",
                "create",
                "Queue",
                f"other-config:min-rate={int(exam_bps * 0.7)}",
                f"other-config:max-rate={exam_bps}",
                "--",
                "--id=@q2",
                "create",
                "Queue",
                "other-config:min-rate=0",
                f"other-config:max-rate={bg_bps}",
            ]
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=8.0,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                self.decision(
                    "queue_fallback_failed",
                    trigger=reason,
                    port=port,
                    error=str(exc),
                )
                break
            if result.returncode != 0:
                self.decision(
                    "queue_fallback_failed",
                    trigger=reason,
                    port=port,
                    error=(result.stderr or result.stdout).strip()[:500],
                )
                break
            configured.append(port)
        if len(configured) != len(ports):
            for port in configured:
                self._run_ovs_clear(port)
            return False
        self.queue_ports = ports
        self.queue_fallback_active = True
        for dpid in self.datapaths:
            self.qos_backend_by_dpid[dpid] = "queue"
        self.decision(
            "queue_fallback_enabled",
            trigger=reason,
            ports=ports,
            exam_rate_mbps=self.cfg.exam_rate_mbps,
            background_rate_mbps=self.cfg.background_rate_mbps,
        )
        return True

    def _run_ovs_clear(self, port: str) -> None:
        try:
            subprocess.run(
                ["ovs-vsctl", "--if-exists", "clear", "Port", port, "qos"],
                capture_output=True,
                text=True,
                timeout=8.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    def _queue_clear_if_needed(self) -> None:
        if not self.queue_fallback_active:
            return
        if self.cfg.queue_clear_on_disable:
            for port in self.queue_ports:
                self._run_ovs_clear(port)
        self.queue_fallback_active = False

    def _qos_hints_for_flow(
        self, datapath: Any, exam: bool
    ) -> tuple[Optional[int], Optional[int]]:
        """Return (meter_id, queue_id) for the datapath's active backend."""
        if not self.qos_active or self.mode != "sdn":
            return None, None
        backend = self.qos_backend_by_dpid.get(datapath.id, "priority")
        if backend == "meter" and datapath.id in self.meters_installed:
            return (
                METER_EXAM if exam else METER_BACKGROUND
            ), None
        if backend == "queue" and self.queue_fallback_active:
            return None, (QUEUE_EXAM if exam else QUEUE_BACKGROUND)
        return None, None

    # ------------------------------------------------------------------
    # Switch connection / baseline rules
    # ------------------------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, event: Any) -> None:
        message = event.msg
        datapath = message.datapath
        with self.lock:
            self._reload_metadata(reason="switch")
            self.datapaths[datapath.id] = datapath
            if datapath.id not in self.meter_support:
                self.meter_support[datapath.id] = None
            self.meter_requested.discard(datapath.id)
            self.meters_installed.discard(datapath.id)
            self.qos_backend_by_dpid[datapath.id] = "priority"
            self._delete_all_flows(datapath)
            self._install_table_miss(datapath)
            if self.mode == "sdn":
                self._install_vip_miss(datapath)
                if self.exam_active:
                    self._request_meter_features_locked()
                    self._install_meters_locked()
                for entry in self.blocked.values():
                    if entry.switch == self._switch_name(datapath.id):
                        self._install_block_flow_locked(entry)
            self.flow_stats_by_dpid[datapath.id] = {
                "flow_count": 0,
                "packet_count": 0,
                "byte_count": 0,
                "categories": {},
                "updated_at": time.time(),
            }
            self.decision(
                "switch_connected",
                switch=self._switch_name(datapath.id),
                dpid=f"{datapath.id:016x}",
                mode=self.mode,
                expected_switch=datapath.id in self.name_by_dpid,
            )
            self.logger.info(
                "switch %s (%016x) connected",
                self._switch_name(datapath.id),
                datapath.id,
            )

    @set_ev_cls(ofp_event.EventOFPMeterFeaturesStatsReply, MAIN_DISPATCHER)
    def meter_features_reply_handler(self, event: Any) -> None:
        datapath = event.msg.datapath
        features = event.msg.body
        with self.lock:
            supported = bool(
                features is not None
                and getattr(features, "max_meter", 0) >= METER_BACKGROUND
                and getattr(features, "band_types", 0)
                & ofproto_v1_3.OFPMBT_DROP
            )
            self.meter_support[datapath.id] = supported
            self.decision(
                "meter_features",
                switch=self._switch_name(datapath.id),
                supported=supported,
                max_meter=getattr(features, "max_meter", None),
                band_types=getattr(features, "band_types", None),
            )
            if supported:
                self.qos_backend_by_dpid[datapath.id] = "meter"
                if self.exam_active and self.qos_active:
                    self._install_meters_on(datapath)
                    self._reset_all_flows_locked(reason="meter-ready")
            elif self.exam_active and self.qos_active:
                if self.cfg.qos_backend != "priority":
                    if self._try_queue_fallback_locked("meter-unsupported"):
                        for dpid in self.datapaths:
                            self.qos_backend_by_dpid[dpid] = "queue"
                        self._reset_all_flows_locked(reason="queue-ready")
                    else:
                        for dpid in self.datapaths:
                            self.qos_backend_by_dpid[dpid] = "priority"
                        self._reset_all_flows_locked(reason="no-meter-no-queue")

    # ------------------------------------------------------------------
    # Packet-in / learning switch
    # ------------------------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, event: Any) -> None:
        message = event.msg
        datapath = message.datapath
        ofp = datapath.ofproto
        in_port = message.match.get("in_port")
        data = message.data or b""
        buffer_id = message.buffer_id
        if in_port is None or not data:
            return

        pkt = packet.Packet(data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None:
            return
        src_mac = str(eth.src).lower()
        dst_mac = str(eth.dst).lower()

        with self.lock:
            self._reload_metadata(reason="packet-in")
            self.packet_in_count += 1
            switch_entry = self._switch_entry(datapath.id)
            src_host = self._host_for_mac(src_mac)
            if src_host is not None:
                self._note_packet_in_locked(src_host)

            if eth.ethertype == ETH_TYPE_ARP:
                arp_pkt = pkt.get_protocol(arp.arp)
                if arp_pkt is None:
                    return
                self._flood(datapath, in_port, data, buffer_id)
                return

            if eth.ethertype != ETH_TYPE_IP:
                self._flood(datapath, in_port, data, buffer_id)
                return

            ip_pkt = pkt.get_protocol(ipv4.ipv4)
            if ip_pkt is None:
                self._flood(datapath, in_port, data, buffer_id)
                return
            tcp_pkt = pkt.get_protocol(tcp.tcp)
            udp_pkt = pkt.get_protocol(udp.udp)
            icmp_pkt = pkt.get_protocol(icmp.icmp)
            dst_ip = str(ip_pkt.dst)
            src_ip = str(ip_pkt.src)

            if self.mode == "sdn" and self.vip_ip and dst_ip == self.vip_ip:
                self._handle_vip_packet_locked(
                    datapath,
                    in_port,
                    data,
                    buffer_id,
                    eth,
                    ip_pkt,
                    tcp_pkt,
                    udp_pkt,
                    icmp_pkt,
                    src_host,
                )
                return

            dst_host = self._host_for_ip(dst_ip)
            out_port: Optional[int] = None
            if switch_entry is not None:
                out_port = self._port_for_route(switch_entry, dst_host)

            if out_port is None:
                # Unknown destination, broadcast, multicast or metadata not
                # loaded yet: flood and rely on the next packet-in.
                self._flood(datapath, in_port, data, buffer_id)
                return

            actions = [parser.OFPActionOutput(out_port)]
            match = self._flow_match(
                in_port=in_port,
                src_ip=src_ip,
                dst_ip=dst_ip,
                ip_proto=int(ip_pkt.proto),
                tcp_pkt=tcp_pkt,
                udp_pkt=udp_pkt,
                icmp_pkt=icmp_pkt,
            )
            priority = PRIORITY_LEARNING
            cookie = COOKIE_LEARNING
            meter_id: Optional[int] = None
            queue_id: Optional[int] = None
            if self.mode == "sdn" and self.qos_active:
                exam = self._is_exam_flow_locked(
                    src_ip, dst_ip, tcp_pkt, udp_pkt
                )
                cookie = COOKIE_EXAM if exam else COOKIE_BACKGROUND
                priority = PRIORITY_EXAM if exam else PRIORITY_BACKGROUND
                meter_id, queue_id = self._qos_hints_for_flow(
                    datapath, exam
                )
            self._add_flow(
                datapath,
                priority=priority,
                match=match,
                actions=actions,
                cookie=cookie,
                meter_id=meter_id,
                queue_id=queue_id,
            )
            self._packet_out(
                datapath, in_port, actions, data, buffer_id
            )

    @staticmethod
    def _flow_match(
        *,
        in_port: int,
        src_ip: str,
        dst_ip: str,
        ip_proto: int,
        tcp_pkt: Any,
        udp_pkt: Any,
        icmp_pkt: Any,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "in_port": in_port,
            "eth_type": ETH_TYPE_IP,
            "ipv4_src": src_ip,
            "ipv4_dst": dst_ip,
            "ip_proto": ip_proto,
        }
        if ip_proto == IPPROTO_TCP and tcp_pkt is not None:
            kwargs["tcp_src"] = int(tcp_pkt.src_port)
            kwargs["tcp_dst"] = int(tcp_pkt.dst_port)
        elif ip_proto == IPPROTO_UDP and udp_pkt is not None:
            kwargs["udp_src"] = int(udp_pkt.src_port)
            kwargs["udp_dst"] = int(udp_pkt.dst_port)
        elif ip_proto == IPPROTO_ICMP:
            if icmp_pkt is not None:
                kwargs["icmpv4_type"] = int(icmp_pkt.type)
        return parser.OFPMatch(**kwargs)

    def _is_exam_flow_locked(
        self,
        src_ip: str,
        dst_ip: str,
        tcp_pkt: Any,
        udp_pkt: Any,
    ) -> bool:
        if self.vip_ip and (src_ip == self.vip_ip or dst_ip == self.vip_ip):
            return True
        if src_ip in self.server_ips or dst_ip in self.server_ips:
            if tcp_pkt is not None:
                if (
                    int(tcp_pkt.src_port) == self.cfg.server_port
                    or int(tcp_pkt.dst_port) == self.cfg.server_port
                ):
                    return True
            if udp_pkt is not None:
                if (
                    int(udp_pkt.src_port) == self.cfg.server_port
                    or int(udp_pkt.dst_port) == self.cfg.server_port
                ):
                    return True
        # An ICMP exchange with a backend is exam monitoring/health, not bulk
        # background traffic.
        if src_ip in self.server_ips or dst_ip in self.server_ips:
            if tcp_pkt is None and udp_pkt is None:
                return True
        return False

    def _note_packet_in_locked(self, host: HostEntry) -> None:
        if self.mode != "sdn" or host.role in ("exam_server", "root"):
            return
        now = time.monotonic()
        window = max(self.cfg.monitor_interval, 1.0)
        events = self.packet_in_by_host.setdefault(host.ip, deque())
        events.append(now)
        cutoff = now - window
        while events and events[0] < cutoff:
            events.popleft()
        if (
            len(events) >= self.cfg.packet_in_threshold
            and host.ip not in self.blocked
        ):
            self._block_host_locked(
                host,
                reason="packet-in-flood",
                rx_bps=0.0,
                rx_pps=float(len(events)) / window,
            )

    # ------------------------------------------------------------------
    # VIP failover and packet handling
    # ------------------------------------------------------------------

    def _handle_vip_packet_locked(
        self,
        datapath: Any,
        in_port: int,
        data: bytes,
        buffer_id: int,
        eth: Any,
        ip_pkt: Any,
        tcp_pkt: Any,
        udp_pkt: Any,
        icmp_pkt: Any,
        src_host: Optional[HostEntry],
    ) -> None:
        backend_ip = self.vip_backend_ip
        backend = self._host_for_ip(backend_ip)
        vip = self.vip_ip
        client = src_host
        if backend is None or vip is None or client is None:
            self._flood(datapath, in_port, data, buffer_id)
            return
        backend_switch = self.switches_by_name.get(backend.switch)
        client_switch = self.switches_by_name.get(client.switch)
        if backend_switch is None or client_switch is None:
            self._flood(datapath, in_port, data, buffer_id)
            return

        src_ip = str(ip_pkt.src)
        dst_ip = str(ip_pkt.dst)
        ip_proto = int(ip_pkt.proto)
        l4 = self._l4_kwargs(ip_proto, tcp_pkt, udp_pkt, icmp_pkt)
        rev_l4 = self._reverse_l4_kwargs(
            ip_proto, tcp_pkt, udp_pkt, icmp_pkt
        )

        backend_mac = backend.mac
        vip_mac = self.vip_mac or backend_mac
        forward_set = [
            parser.OFPActionSetField(eth_dst=backend_mac),
            parser.OFPActionSetField(ipv4_dst=backend.ip),
        ]
        reverse_set = [
            parser.OFPActionSetField(eth_src=vip_mac),
            parser.OFPActionSetField(ipv4_src=vip),
        ]

        same_switch = client.switch == backend.switch
        # ---- request path ----
        if same_switch:
            out = backend.port
            if out is not None:
                actions = forward_set + [parser.OFPActionOutput(out)]
                match = parser.OFPMatch(
                    in_port=int(in_port),
                    eth_type=ETH_TYPE_IP,
                    ipv4_src=src_ip,
                    ipv4_dst=vip,
                    ip_proto=ip_proto,
                    **l4,
                )
                self._add_flow(
                    datapath,
                    priority=PRIORITY_VIP_FLOW,
                    match=match,
                    actions=actions,
                    cookie=COOKIE_VIP_FLOW,
                )
                self.vip_flow_count += 1
            current_actions = (
                forward_set + [parser.OFPActionOutput(out)]
                if out is not None
                else [parser.OFPActionOutput(datapath.ofproto.OFPP_FLOOD)]
            )
        else:
            client_out = client_switch.bottleneck_port
            server_out = backend.port
            if client_out is not None:
                client_actions = forward_set + [
                    parser.OFPActionOutput(client_out)
                ]
                client_match = parser.OFPMatch(
                    in_port=client.port if client.port is not None else int(in_port),
                    eth_type=ETH_TYPE_IP,
                    ipv4_src=src_ip,
                    ipv4_dst=vip,
                    ip_proto=ip_proto,
                    **l4,
                )
                self._add_flow(
                    self.datapaths.get(
                        self._dpid_for_switch(client.switch), datapath
                    ),
                    priority=PRIORITY_VIP_FLOW,
                    match=client_match,
                    actions=client_actions,
                    cookie=COOKIE_VIP_FLOW,
                )
                self.vip_flow_count += 1
            if server_out is not None:
                server_match = parser.OFPMatch(
                    in_port=(
                        backend_switch.bottleneck_port
                        if backend_switch.bottleneck_port is not None
                        else int(in_port)
                    ),
                    eth_type=ETH_TYPE_IP,
                    ipv4_src=src_ip,
                    ipv4_dst=backend.ip,
                    ip_proto=ip_proto,
                    **l4,
                )
                server_actions = forward_set + [
                    parser.OFPActionOutput(server_out)
                ]
                # The set-field actions are harmless on the server side; the
                # client-side rule already rewrote the packet.  Keeping them
                # makes the rule work after a switch restart as well.
                self._add_flow(
                    self.datapaths.get(
                        self._dpid_for_switch(backend.switch), datapath
                    ),
                    priority=PRIORITY_VIP_FLOW,
                    match=server_match,
                    actions=server_actions,
                    cookie=COOKIE_VIP_FLOW,
                )
                self.vip_flow_count += 1
            current_actions = (
                forward_set + [parser.OFPActionOutput(client_out)]
                if datapath.id == self._dpid_for_switch(client.switch)
                and client_out is not None
                else (
                    forward_set + [parser.OFPActionOutput(server_out)]
                    if server_out is not None
                    else [parser.OFPActionOutput(datapath.ofproto.OFPP_FLOOD)]
                )
            )

        # ---- return path ----
        if same_switch:
            if client.port is not None:
                match = parser.OFPMatch(
                    in_port=backend.port,
                    eth_type=ETH_TYPE_IP,
                    ipv4_src=backend.ip,
                    ipv4_dst=src_ip,
                    ip_proto=ip_proto,
                    **rev_l4,
                )
                actions = reverse_set + [
                    parser.OFPActionOutput(client.port)
                ]
                self._add_flow(
                    datapath,
                    priority=PRIORITY_VIP_FLOW,
                    match=match,
                    actions=actions,
                    cookie=COOKIE_VIP_FLOW,
                )
                self.vip_flow_count += 1
        else:
            client_dpid = self._dpid_for_switch(client.switch)
            server_dpid = self._dpid_for_switch(backend.switch)
            client_dp = self.datapaths.get(client_dpid)
            server_dp = self.datapaths.get(server_dpid)
            if (
                client_dp is not None
                and client_switch.bottleneck_port is not None
                and client.port is not None
            ):
                match = parser.OFPMatch(
                    in_port=client_switch.bottleneck_port,
                    eth_type=ETH_TYPE_IP,
                    ipv4_src=vip,
                    ipv4_dst=src_ip,
                    ip_proto=ip_proto,
                    **rev_l4,
                )
                actions = [parser.OFPActionOutput(client.port)]
                self._add_flow(
                    client_dp,
                    priority=PRIORITY_VIP_FLOW,
                    match=match,
                    actions=actions,
                    cookie=COOKIE_VIP_FLOW,
                )
                self.vip_flow_count += 1
            if (
                server_dp is not None
                and backend.port is not None
                and backend_switch.bottleneck_port is not None
            ):
                match = parser.OFPMatch(
                    in_port=backend.port,
                    eth_type=ETH_TYPE_IP,
                    ipv4_src=backend.ip,
                    ipv4_dst=src_ip,
                    ip_proto=ip_proto,
                    **rev_l4,
                )
                actions = reverse_set + [
                    parser.OFPActionOutput(
                        backend_switch.bottleneck_port
                    )
                ]
                self._add_flow(
                    server_dp,
                    priority=PRIORITY_VIP_FLOW,
                    match=match,
                    actions=actions,
                    cookie=COOKIE_VIP_FLOW,
                )
                self.vip_flow_count += 1

        self._packet_out(
            datapath, int(in_port), current_actions, data, buffer_id
        )
        self.decision(
            "vip_flow",
            client=client.name,
            client_ip=src_ip,
            vip=vip,
            backend=self._server_name(backend.ip),
            backend_ip=backend.ip,
            proto=ip_proto,
            in_switch=self._switch_name(datapath.id),
        )

    @staticmethod
    def _l4_kwargs(
        ip_proto: int, tcp_pkt: Any, udp_pkt: Any, icmp_pkt: Any
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if ip_proto == IPPROTO_TCP and tcp_pkt is not None:
            kwargs["tcp_src"] = int(tcp_pkt.src_port)
            kwargs["tcp_dst"] = int(tcp_pkt.dst_port)
        elif ip_proto == IPPROTO_UDP and udp_pkt is not None:
            kwargs["udp_src"] = int(udp_pkt.src_port)
            kwargs["udp_dst"] = int(udp_pkt.dst_port)
        elif ip_proto == IPPROTO_ICMP and icmp_pkt is not None:
            kwargs["icmpv4_type"] = int(icmp_pkt.type)
        return kwargs

    @staticmethod
    def _reverse_l4_kwargs(
        ip_proto: int, tcp_pkt: Any, udp_pkt: Any, icmp_pkt: Any
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if ip_proto == IPPROTO_TCP and tcp_pkt is not None:
            kwargs["tcp_src"] = int(tcp_pkt.dst_port)
            kwargs["tcp_dst"] = int(tcp_pkt.src_port)
        elif ip_proto == IPPROTO_UDP and udp_pkt is not None:
            kwargs["udp_src"] = int(udp_pkt.dst_port)
            kwargs["udp_dst"] = int(udp_pkt.src_port)
        elif ip_proto == IPPROTO_ICMP and icmp_pkt is not None:
            # Echo reply has a different type; use only the protocol so the
            # same rule covers the reply direction.
            pass
        return kwargs

    def _dpid_for_switch(self, name: str) -> int:
        entry = self.switches_by_name.get(name)
        if entry is None:
            return 0
        return entry.dpid

    def _failover_vip_locked(
        self, new_ip: str, reason: str
    ) -> None:
        old_ip = self.vip_backend_ip
        if new_ip == old_ip:
            return
        self.vip_backend_ip = new_ip
        self.vip_failovers += 1
        now = time.time()
        self.vip_last_failover_at = now
        for datapath in list(self.datapaths.values()):
            self._delete_cookie(datapath, COOKIE_VIP_FLOW)
        self.vip_flow_count = 0
        self.decision(
            "vip_failover",
            old_ip=old_ip,
            old_server=self._server_name(old_ip),
            new_ip=new_ip,
            new_server=self._server_name(new_ip),
            reason=reason,
            failovers=self.vip_failovers,
        )
        self.logger.warning(
            "VIP failover: %s -> %s (%s)",
            self._server_name(old_ip) or old_ip,
            self._server_name(new_ip) or new_ip,
            reason,
        )

    # ------------------------------------------------------------------
    # Health probes
    # ------------------------------------------------------------------

    def _probe_server(self, ip: str) -> tuple[bool, Optional[float], Optional[dict[str, Any]], Optional[str]]:
        started = time.monotonic()
        conn: Optional[socket.socket] = None
        try:
            conn = socket.create_connection(
                (ip, self.cfg.server_port), timeout=self.cfg.health_timeout
            )
            conn.settimeout(self.cfg.health_timeout)
            request = (
                "GET /health HTTP/1.1\r\n"
                f"Host: {ip}:{self.cfg.server_port}\r\n"
                "Connection: close\r\n"
                "User-Agent: exam-sdn-controller/1\r\n"
                "\r\n"
            ).encode("ascii")
            conn.sendall(request)
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                total += len(chunk)
                if total > 64 * 1024:
                    break
                chunks.append(chunk)
            raw = b"".join(chunks)
            latency_ms = round((time.monotonic() - started) * 1000.0, 2)
            first_line = raw.split(b"\r\n", 1)[0]
            fields = first_line.split(b" ")
            if len(fields) < 2 or not fields[1].isdigit():
                return False, latency_ms, None, "malformed response"
            if int(fields[1]) != 200:
                return False, latency_ms, None, f"HTTP {int(fields[1])}"
            body = raw.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in raw else b""
            parsed: Optional[dict[str, Any]] = None
            try:
                candidate = json.loads(body.decode("utf-8"))
                if isinstance(candidate, dict):
                    parsed = candidate
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            return True, latency_ms, parsed, None
        except OSError as exc:
            return False, None, None, str(exc)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass

    def _health_loop(self) -> None:
        while not self._stop_event.is_set():
            interval = self.cfg.health_interval
            try:
                with self.lock:
                    metadata_ok = self.metadata is not None
                    mode = self.mode
                    servers = list(self.server_order)
                if metadata_ok and mode == "sdn" and servers:
                    results = {
                        ip: self._probe_server(ip) for ip in servers
                    }
                    with self.lock:
                        self._apply_health_results_locked(results)
            except Exception:  # noqa: BLE001 - keep probing
                self.logger.exception("health loop failed")
            if self._stop_event.wait(interval):
                return

    def _apply_health_results_locked(
        self, results: dict[str, tuple[bool, Optional[float], Optional[dict[str, Any]], Optional[str]]]
    ) -> None:
        now = time.time()
        for ip, (ok, latency, parsed, error) in results.items():
            state = self.server_health.setdefault(
                ip, self._server_health_default()
            )
            state["last_probe_at"] = now
            state["latency_ms"] = latency
            state["probe_attempts"] = int(state.get("probe_attempts", 0)) + 1
            if ok:
                was_healthy = state.get("healthy")
                state["healthy"] = True
                state["last_ok_at"] = now
                state["consecutive_failures"] = 0
                state["last_error"] = None
                previous_attempts = self.server_attempts.get(ip)
                if parsed is not None:
                    attempts = parsed.get("active_attempts")
                    if (
                        isinstance(attempts, int)
                        and not isinstance(attempts, bool)
                        and attempts >= 0
                    ):
                        self.server_attempts[ip] = attempts
                if was_healthy is not True:
                    self.decision(
                        "server_health",
                        server=ip,
                        name=self._server_name(ip),
                        healthy=True,
                        latency_ms=latency,
                    )
                if previous_attempts != self.server_attempts.get(ip):
                    self._refresh_exam_active_locked(event="health_probe")
            else:
                state["consecutive_failures"] = (
                    int(state.get("consecutive_failures", 0)) + 1
                )
                state["last_error"] = error
                state["probe_failures"] = int(
                    state.get("probe_failures", 0)
                ) + 1
                if (
                    state["consecutive_failures"]
                    >= self.cfg.health_failures
                ):
                    if state.get("healthy") is not False:
                        self.decision(
                            "server_health",
                            server=ip,
                            name=self._server_name(ip),
                            healthy=False,
                            failures=state["consecutive_failures"],
                            error=error,
                        )
                    state["healthy"] = False
                    if self.server_attempts.get(ip):
                        self.server_attempts[ip] = 0
                        self._refresh_exam_active_locked(event="health_probe")

        if not self.server_order:
            return
        active = self.vip_backend_ip
        if active not in self.server_ips:
            active = self.server_order[0]
            self.vip_backend_ip = active
        active_state = self.server_health.get(active, {})
        active_healthy = active_state.get("healthy") is True
        if not active_healthy and (
            active_state.get("consecutive_failures", 0)
            >= self.cfg.health_failures
        ):
            candidates = [
                ip
                for ip in self.server_order
                if ip != active
                and self.server_health.get(ip, {}).get("healthy") is True
            ]
            if candidates:
                self._failover_vip_locked(
                    candidates[0],
                    reason=(
                        f"{self._server_name(active) or active} "
                        f"health check failed"
                    ),
                )

    # ------------------------------------------------------------------
    # Periodic monitoring, statistics and abuse blocking
    # ------------------------------------------------------------------

    def _monitor_loop(self) -> None:
        while not self._stop_event.is_set():
            interval = self.cfg.monitor_interval
            try:
                self._monitor_tick()
            except Exception:  # noqa: BLE001 - keep the demo alive
                self.logger.exception("monitor tick failed")
            if self._stop_event.wait(interval):
                return

    def _monitor_tick(self) -> None:
        with self.lock:
            self._reload_metadata(reason="monitor")
            self._expire_blocks_locked()
            if self.mode != "sdn":
                return
            for datapath in list(self.datapaths.values()):
                try:
                    request = datapath.ofproto_parser.OFPFlowStatsRequest(
                        datapath,
                        table_id=datapath.ofproto.OFPTT_ALL,
                        out_port=datapath.ofproto.OFPP_ANY,
                        out_group=datapath.ofproto.OFPG_ANY,
                        cookie=0,
                        cookie_mask=0,
                        match=datapath.ofproto_parser.OFPMatch(),
                    )
                    self._send(datapath, request)
                    port_request = datapath.ofproto_parser.OFPPortStatsRequest(
                        datapath,
                        port_no=datapath.ofproto.OFPP_ANY,
                    )
                    self._send(datapath, port_request)
                    if (
                        datapath.id in self.meters_installed
                        and self.qos_backend_by_dpid.get(datapath.id)
                        == "meter"
                    ):
                        meter_request = (
                            datapath.ofproto_parser.OFPMeterStatsRequest(
                                datapath,
                                meter_id=datapath.ofproto.OFPM_ALL,
                            )
                        )
                        self._send(datapath, meter_request)
                except Exception as exc:  # noqa: BLE001
                    self.logger.debug(
                        "stats request to %s failed: %s",
                        self._switch_name(datapath.id),
                        exc,
                    )

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply_handler(self, event: Any) -> None:
        datapath = event.msg.datapath
        with self.lock:
            if self.mode != "sdn":
                return
            flow_count = 0
            packet_count = 0
            byte_count = 0
            categories: dict[str, dict[str, int]] = {}
            for stat in event.msg.body:
                flow_count += 1
                packet_count += int(stat.packet_count or 0)
                byte_count += int(stat.byte_count or 0)
                name = COOKIE_NAMES.get(int(stat.cookie), "other")
                bucket = categories.setdefault(
                    name, {"flows": 0, "packets": 0, "bytes": 0}
                )
                bucket["flows"] += 1
                bucket["packets"] += int(stat.packet_count or 0)
                bucket["bytes"] += int(stat.byte_count or 0)
            self.flow_stats_by_dpid[datapath.id] = {
                "flow_count": flow_count,
                "packet_count": packet_count,
                "byte_count": byte_count,
                "categories": categories,
                "updated_at": time.time(),
            }
            self.last_flow_stats_at = time.time()

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, event: Any) -> None:
        datapath = event.msg.datapath
        now_mono = time.monotonic()
        now_wall = time.time()
        with self.lock:
            if self.mode != "sdn":
                return
            switch_entry = self._switch_entry(datapath.id)
            if switch_entry is None:
                return
            hosts_by_port = {
                entry.port: entry
                for entry in self.hosts_by_switch.get(
                    switch_entry.name, []
                )
                if entry.port is not None
            }
            for stat in event.msg.body:
                port_no = int(stat.port_no)
                current = PortSample(
                    at=now_mono,
                    rx_bytes=int(stat.rx_bytes),
                    rx_packets=int(stat.rx_packets),
                    tx_bytes=int(stat.tx_bytes),
                    tx_packets=int(stat.tx_packets),
                    rx_dropped=int(getattr(stat, "rx_dropped", 0) or 0),
                    tx_dropped=int(getattr(stat, "tx_dropped", 0) or 0),
                )
                previous = self.port_samples.get(
                    (datapath.id, port_no)
                )
                self.port_samples[(datapath.id, port_no)] = current
                host = hosts_by_port.get(port_no)
                if host is None:
                    continue
                rx_bps = rx_pps = tx_bps = tx_pps = 0.0
                if previous is not None:
                    elapsed = current.at - previous.at
                    if elapsed > 0.001:
                        rx_bps = max(
                            0.0,
                            (current.rx_bytes - previous.rx_bytes) / elapsed,
                        )
                        rx_pps = max(
                            0.0,
                            (current.rx_packets - previous.rx_packets)
                            / elapsed,
                        )
                        tx_bps = max(
                            0.0,
                            (current.tx_bytes - previous.tx_bytes) / elapsed,
                        )
                        tx_pps = max(
                            0.0,
                            (current.tx_packets - previous.tx_packets)
                            / elapsed,
                        )
                self.host_metrics[host.ip] = {
                    "name": host.name,
                    "ip": host.ip,
                    "switch": host.switch,
                    "port": port_no,
                    "rx_bytes": current.rx_bytes,
                    "rx_packets": current.rx_packets,
                    "tx_bytes": current.tx_bytes,
                    "tx_packets": current.tx_packets,
                    "rx_bytes_per_s": round(rx_bps, 1),
                    "rx_packets_per_s": round(rx_pps, 1),
                    "tx_bytes_per_s": round(tx_bps, 1),
                    "tx_packets_per_s": round(tx_pps, 1),
                    "rx_dropped": current.rx_dropped,
                    "tx_dropped": current.tx_dropped,
                    "updated_at": now_wall,
                }
                if previous is not None and host.role not in (
                    "exam_server",
                    "root",
                ):
                    if (
                        host.ip not in self.blocked
                        and (
                            rx_bps >= self.cfg.abuse_bytes_per_sec
                            or rx_pps >= self.cfg.abuse_packets_per_sec
                        )
                    ):
                        self._block_host_locked(
                            host,
                            reason="ingress-rate",
                            rx_bps=rx_bps,
                            rx_pps=rx_pps,
                        )
            self.last_port_stats_at = now_wall

    @set_ev_cls(ofp_event.EventOFPMeterStatsReply, MAIN_DISPATCHER)
    def meter_stats_reply_handler(self, event: Any) -> None:
        datapath = event.msg.datapath
        now = time.time()
        with self.lock:
            for stat in event.msg.body:
                meter_id = int(stat.meter_id)
                self.meter_samples[(datapath.id, meter_id)] = MeterSample(
                    meter_id=meter_id,
                    flow_count=int(stat.flow_count or 0),
                    packet_count=int(stat.packet_in_count or 0),
                    byte_count=int(stat.byte_in_count or 0),
                    at=now,
                )
            self.last_meter_stats_at = now

    @set_ev_cls(ofp_event.EventOFPErrorMsg, MAIN_DISPATCHER)
    def error_msg_handler(self, event: Any) -> None:
        message = event.msg
        with self.lock:
            detail = getattr(message, "data", b"")
            text = bytes(detail[:128]).hex() if detail else ""
            error_type = int(getattr(message, "type", 0))
            self.decision(
                "openflow_error",
                switch=self._switch_name(message.datapath.id),
                type=error_type,
                code=int(getattr(message, "code", 0)),
                data=text,
            )
            if error_type == ofproto_v1_3.OFPET_METER_MOD_FAILED:
                datapath = message.datapath
                self.meter_support[datapath.id] = False
                self.meters_installed.discard(datapath.id)
                self.qos_backend_by_dpid[datapath.id] = "priority"
                if self.exam_active and self.qos_active:
                    if self._try_queue_fallback_locked("meter-error-event"):
                        for dpid in self.datapaths:
                            self.qos_backend_by_dpid[dpid] = "queue"
                        self._reset_all_flows_locked(reason="queue-after-error")
                    else:
                        self._reset_all_flows_locked(reason="priority-after-error")
            self.logger.warning(
                "OpenFlow error from %s: type=%s code=%s",
                self._switch_name(message.datapath.id),
                getattr(message, "type", "?"),
                getattr(message, "code", "?"),
            )

    def _block_host_locked(
        self,
        host: HostEntry,
        *,
        reason: str,
        rx_bps: float,
        rx_pps: float,
    ) -> None:
        if self.mode != "sdn" or host.ip in self.blocked:
            return
        switch_entry = self.switches_by_name.get(host.switch)
        if switch_entry is None:
            return
        datapath = self.datapaths.get(switch_entry.dpid)
        if datapath is None:
            return
        self.block_sequence = (self.block_sequence + 1) & 0xFF
        cookie = COOKIE_BLOCK | self.block_sequence
        now = time.time()
        entry = BlockEntry(
            host=host.name,
            ip=host.ip,
            mac=host.mac,
            switch=host.switch,
            port=host.port,
            reason=reason,
            started_at=now,
            expires_at=now + self.cfg.block_seconds,
            cookie=cookie,
            rx_bps=round(rx_bps, 1),
            rx_pps=round(rx_pps, 1),
        )
        self.blocked[host.ip] = entry
        self._install_block_flow_locked(entry)
        self.decision(
            "host_blocked",
            host=host.name,
            ip=host.ip,
            switch=host.switch,
            port=host.port,
            reason=reason,
            rx_bytes_per_s=entry.rx_bps,
            rx_packets_per_s=entry.rx_pps,
            seconds=self.cfg.block_seconds,
            expires_at=round(entry.expires_at, 3),
        )
        self.logger.warning(
            "blocked %s (%s) for %.0fs: %s",
            host.name,
            host.ip,
            self.cfg.block_seconds,
            reason,
        )

    def _install_block_flow_locked(self, entry: BlockEntry) -> None:
        switch_entry = self.switches_by_name.get(entry.switch)
        if switch_entry is None:
            return
        datapath = self.datapaths.get(switch_entry.dpid)
        if datapath is None:
            return
        if entry.port is not None:
            match = parser.OFPMatch(in_port=entry.port)
        else:
            match = parser.OFPMatch(eth_src=entry.mac)
        remaining = max(1, int(round(entry.expires_at - time.time())))
        self._add_flow(
            datapath,
            priority=PRIORITY_BLOCK,
            match=match,
            actions=[],
            cookie=entry.cookie,
            idle_timeout=0,
            hard_timeout=remaining,
        )

    def _expire_blocks_locked(self) -> None:
        now = time.time()
        expired = [
            entry for entry in self.blocked.values()
            if entry.expires_at <= now
        ]
        for entry in expired:
            switch_entry = self.switches_by_name.get(entry.switch)
            if switch_entry is not None:
                datapath = self.datapaths.get(switch_entry.dpid)
                if datapath is not None:
                    self._delete_cookie(datapath, entry.cookie)
            self.blocked.pop(entry.ip, None)
            self.decision(
                "host_unblocked",
                host=entry.host,
                ip=entry.ip,
                reason="timeout",
                blocked_seconds=round(now - entry.started_at, 1),
            )
            self.logger.info("unblocked %s (%s) after timeout", entry.host, entry.ip)

    def _clear_blocks_locked(
        self, reason: str, delete_flows: bool = True
    ) -> None:
        for entry in list(self.blocked.values()):
            if delete_flows:
                switch_entry = self.switches_by_name.get(entry.switch)
                if switch_entry is not None:
                    datapath = self.datapaths.get(switch_entry.dpid)
                    if datapath is not None:
                        self._delete_cookie(datapath, entry.cookie)
            self.decision(
                "host_unblocked",
                host=entry.host,
                ip=entry.ip,
                reason=reason,
                blocked_seconds=round(time.time() - entry.started_at, 1),
            )
        self.blocked.clear()
        self.packet_in_by_host.clear()

    # ------------------------------------------------------------------
    # State snapshots
    # ------------------------------------------------------------------

    def state_snapshot(self) -> dict[str, Any]:
        with self.lock:
            self._reload_metadata(reason="state")
            now = time.time()
            connected = []
            for dpid, datapath in self.datapaths.items():
                expected = dpid in self.name_by_dpid
                connected.append(
                    {
                        "name": self._switch_name(dpid),
                        "dpid": f"{dpid:016x}",
                        "expected": expected,
                    }
                )
            expected_switches = sorted(self.switches_by_name)
            server_state: dict[str, Any] = {}
            for ip in self.server_order:
                host = self._host_for_ip(ip)
                health = self.server_health.get(
                    ip, self._server_health_default()
                )
                server_state[self._server_name(ip) or ip] = {
                    "ip": ip,
                    "healthy": health.get("healthy"),
                    "vip_backend": ip == self.vip_backend_ip,
                    "active_attempts": self.server_attempts.get(ip, 0),
                    "consecutive_failures": health.get(
                        "consecutive_failures", 0
                    ),
                    "last_probe_at": _wall_iso(health.get("last_probe_at")),
                    "last_ok_at": _wall_iso(health.get("last_ok_at")),
                    "latency_ms": health.get("latency_ms"),
                    "last_error": health.get("last_error"),
                    "last_event_at": _wall_iso(
                        self.server_last_event.get(ip)
                    ),
                }
            blocked = sorted(
                (
                    {
                        "host": entry.host,
                        "ip": entry.ip,
                        "switch": entry.switch,
                        "port": entry.port,
                        "reason": entry.reason,
                        "blocked_at": _wall_iso(entry.started_at),
                        "expires_at": _wall_iso(entry.expires_at),
                        "remaining_s": round(
                            max(0.0, entry.expires_at - now), 1
                        ),
                        "rx_bytes_per_s": entry.rx_bps,
                        "rx_packets_per_s": entry.rx_pps,
                    }
                    for entry in self.blocked.values()
                ),
                key=lambda item: item["host"],
            )
            qos_switches = {
                self._switch_name(dpid): {
                    "backend": backend,
                    "meters_installed": dpid in self.meters_installed,
                }
                for dpid, backend in self.qos_backend_by_dpid.items()
            }
            backends = {
                item["backend"] for item in qos_switches.values()
            }
            if self.queue_fallback_active:
                backend_summary = "queue"
            elif "meter" in backends:
                backend_summary = "meter"
            elif "priority" in backends:
                backend_summary = "priority"
            else:
                backend_summary = "none"
            desired_switches = expected_switches or [
                self._switch_name(dpid) for dpid in self.datapaths
            ]
            return {
                "mode": self.mode,
                "mode_changed_at": _wall_iso(self.mode_changed_at),
                "uptime_s": round(now - self.started_at, 3),
                "metadata": {
                    "loaded": self.metadata is not None,
                    "path": str(self.cfg.metadata_path),
                    "loaded_at": _wall_iso(self.metadata_loaded_at),
                    "error": self.metadata_error,
                },
                "switches": {
                    "expected": desired_switches,
                    "connected": connected,
                    "all_connected": bool(desired_switches)
                    and len(
                        {
                            item["name"] for item in connected
                        }
                        & set(desired_switches)
                    )
                    == len(desired_switches),
                },
                "exam": {
                    "active": self.exam_active,
                    "lifecycle_active": bool(
                        self.exam_lifecycle_active
                    ),
                    "exam_id": self.exam_id,
                    "active_attempts": self._total_attempts_locked(),
                    "active_changed_at": _wall_iso(
                        self.exam_active_changed_at
                    ),
                    "servers": self._server_active_attempts_locked(),
                    "events_received": self.event_count,
                    "event_counts": dict(sorted(self.event_counts.items())),
                    "last_event": self.last_event,
                    "last_event_at": _wall_iso(self.last_event_at),
                },
                "qos": {
                    "active": bool(self.qos_active and self.mode == "sdn"),
                    "backend": backend_summary,
                    "requested_backend": self.cfg.qos_backend,
                    "queue_fallback_active": self.queue_fallback_active,
                    "queue_fallback_attempted": self.queue_fallback_attempted,
                    "exam_rate_mbps": self.cfg.exam_rate_mbps,
                    "background_rate_mbps": self.cfg.background_rate_mbps,
                    "switches": qos_switches,
                },
                "vip": {
                    "ip": self.vip_ip,
                    "mac": self.vip_mac,
                    "backend_ip": self.vip_backend_ip,
                    "backend": self._server_name(self.vip_backend_ip),
                    "failovers": self.vip_failovers,
                    "last_failover_at": _wall_iso(
                        self.vip_last_failover_at
                    ),
                    "flows_installed": self.vip_flow_count,
                },
                "servers": server_state,
                "monitoring": {
                    "enabled": self.mode == "sdn",
                    "interval_s": self.cfg.monitor_interval,
                    "last_flow_stats_at": _wall_iso(
                        self.last_flow_stats_at
                    ),
                    "last_port_stats_at": _wall_iso(
                        self.last_port_stats_at
                    ),
                    "abuse_bytes_per_s": self.cfg.abuse_bytes_per_sec,
                    "abuse_packets_per_s": self.cfg.abuse_packets_per_sec,
                },
                "blocked_hosts": blocked,
                "rest": {
                    "host": self.cfg.rest_host,
                    "port": self._rest.bound_port,
                },
            }

    def stats_snapshot(self) -> dict[str, Any]:
        with self.lock:
            now = time.time()
            switches: dict[str, Any] = {}
            total_flows = 0
            total_packets = 0
            total_bytes = 0
            for dpid, datapath in self.datapaths.items():
                switch_name = self._switch_name(dpid)
                flow_stat = self.flow_stats_by_dpid.get(
                    dpid,
                    {
                        "flow_count": 0,
                        "packet_count": 0,
                        "byte_count": 0,
                        "categories": {},
                        "updated_at": None,
                    },
                )
                total_flows += int(flow_stat.get("flow_count", 0))
                total_packets += int(flow_stat.get("packet_count", 0))
                total_bytes += int(flow_stat.get("byte_count", 0))
                ports: dict[str, Any] = {}
                for host in self.hosts_by_switch.get(switch_name, []):
                    metric = self.host_metrics.get(host.ip)
                    if metric is None:
                        metric = {
                            "name": host.name,
                            "ip": host.ip,
                            "switch": host.switch,
                            "port": host.port,
                            "rx_bytes_per_s": 0.0,
                            "rx_packets_per_s": 0.0,
                            "tx_bytes_per_s": 0.0,
                            "tx_packets_per_s": 0.0,
                        }
                    entry = dict(metric)
                    entry["blocked"] = host.ip in self.blocked
                    ports[host.name] = entry
                meters = {
                    str(meter_id): {
                        "flow_count": sample.flow_count,
                        "packet_count": sample.packet_count,
                        "byte_count": sample.byte_count,
                        "updated_at": _wall_iso(sample.at),
                    }
                    for (sample_dpid, meter_id), sample in self.meter_samples.items()
                    if sample_dpid == dpid
                }
                switches[switch_name] = {
                    "dpid": f"{dpid:016x}",
                    "connected": True,
                    "flows": flow_stat.get("flow_count", 0),
                    "packets": flow_stat.get("packet_count", 0),
                    "bytes": flow_stat.get("byte_count", 0),
                    "categories": flow_stat.get("categories", {}),
                    "updated_at": _wall_iso(flow_stat.get("updated_at")),
                    "ports": ports,
                    "meters": meters,
                }
            for expected in self.switches_by_name:
                if expected not in switches:
                    switches[expected] = {
                        "dpid": f"{self.switches_by_name[expected].dpid:016x}",
                        "connected": False,
                        "flows": 0,
                        "packets": 0,
                        "bytes": 0,
                        "categories": {},
                        "updated_at": None,
                        "ports": {
                            host.name: {
                                "name": host.name,
                                "ip": host.ip,
                                "switch": host.switch,
                                "port": host.port,
                                "rx_bytes_per_s": 0.0,
                                "rx_packets_per_s": 0.0,
                                "tx_bytes_per_s": 0.0,
                                "tx_packets_per_s": 0.0,
                                "blocked": host.ip in self.blocked,
                            }
                            for host in self.hosts_by_switch.get(
                                expected, []
                            )
                        },
                        "meters": {},
                    }
            return {
                "mode": self.mode,
                "generated_at": _wall_iso(now),
                "monitoring": {
                    "enabled": self.mode == "sdn",
                    "interval_s": self.cfg.monitor_interval,
                    "last_flow_stats_at": _wall_iso(
                        self.last_flow_stats_at
                    ),
                    "last_port_stats_at": _wall_iso(
                        self.last_port_stats_at
                    ),
                    "last_meter_stats_at": _wall_iso(
                        self.last_meter_stats_at
                    ),
                    "packet_ins": self.packet_in_count,
                },
                "totals": {
                    "flows": total_flows,
                    "packets": total_packets,
                    "bytes": total_bytes,
                    "blocked_hosts": len(self.blocked),
                    "packet_ins": self.packet_in_count,
                },
                "switches": switches,
                "blocked_hosts": sorted(
                    entry.host for entry in self.blocked.values()
                ),
            }



