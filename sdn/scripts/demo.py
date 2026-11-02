#!/usr/bin/env python3
"""Run the end-to-end SDN demo on the local Mininet topology.

The script is intentionally self-contained:

1. starts the controller if it is not already listening,
2. builds the topology,
3. starts two stdlib stub backends and an iperf3 server,
4. checks ``pingall`` in static and SDN mode,
5. compares VIP HTTP latency under background iperf,
6. starts a UDP flood from ``atk`` and verifies block/unblock,
7. stops ``srv1`` and verifies VIP failover to ``srv2``,
8. saves state/stats/log artifacts and prints a summary.

Run it as root from any directory::

    sudo python3 sdn/scripts/demo.py --students 10

Only the Python standard library is used by this script.  The real C exam
server can be used instead of the stubs, but the stubs make the demo
reproducible without building or seeding a database.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[2]
RUN_DIR = ROOT / "run"
DEMO_DIR = RUN_DIR / "demo"
METADATA = RUN_DIR / "topology.json"
STUB = ROOT / "sdn" / "stub_server.py"
FLOOD = ROOT / "sdn" / "scripts" / "flood.py"
PROBE = ROOT / "sdn" / "scripts" / "latency_probe.py"
CONTROLLER = ROOT / "sdn" / "scripts" / "run_controller.sh"
VENV_PYTHON = ROOT / "sdn" / ".venv" / "bin" / "python"

VIP = "10.0.0.100"
SRV1 = "10.0.0.1"
SRV2 = "10.0.0.2"
BG1 = "10.0.0.201"
BG2 = "10.0.0.202"
ATK = "10.0.0.250"
SERVER_PORT = 8080
IPERF_PORT = 5201


def http_json(
    host: str,
    port: int,
    method: str,
    path: str,
    payload: Optional[dict[str, Any]] = None,
    timeout: float = 3.0,
) -> tuple[int, dict[str, Any]]:
    body = (
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if payload is not None
        else b""
    )
    request = (
        f"{method} {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii") + body
    conn = socket.create_connection((host, port), timeout=timeout)
    try:
        conn.settimeout(timeout)
        conn.sendall(request)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > 4 * 1024 * 1024:
                raise ValueError("controller response too large")
            chunks.append(chunk)
        raw = b"".join(chunks)
        header, _, response_body = raw.partition(b"\r\n\r\n")
        first_line = header.split(b"\r\n", 1)[0].split(b" ")
        if len(first_line) < 2 or not first_line[1].isdigit():
            raise ValueError("malformed controller response")
        status = int(first_line[1])
        parsed: dict[str, Any] = {}
        if response_body.strip():
            candidate = json.loads(response_body.decode("utf-8"))
            if isinstance(candidate, dict):
                parsed = candidate
        return status, parsed
    finally:
        try:
            conn.close()
        except OSError:
            pass


def wait_until(
    predicate: Callable[[], Any],
    timeout: float,
    interval: float = 0.5,
    description: str = "condition",
):
    deadline = time.monotonic() + timeout
    last_error: Optional[BaseException] = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except Exception as exc:  # noqa: BLE001 - report the last error
            last_error = exc
        time.sleep(interval)
    if last_error is not None:
        raise TimeoutError(f"timed out waiting for {description}: {last_error}")
    raise TimeoutError(f"timed out waiting for {description}")


class Demo:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.rest_bind = args.rest_host
        # When the controller binds to a wildcard address, local REST calls
        # must still use a concrete loopback address.
        self.rest_host = (
            "127.0.0.1"
            if self.rest_bind in ("0.0.0.0", "::", "")
            else self.rest_bind
        )
        self.rest_port = args.rest_port
        self.net: Any = None
        self.controller_proc: Optional[subprocess.Popen[Any]] = None
        self.owns_controller = False
        self.stub_procs: dict[str, subprocess.Popen[Any]] = {}
        self.iperf_server: Optional[subprocess.Popen[Any]] = None
        self.iperf_client: Optional[subprocess.Popen[Any]] = None
        self.flood_proc: Optional[subprocess.Popen[Any]] = None
        self.artifacts: dict[str, str] = {}
        self.summary: dict[str, Any] = {
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "args": vars(args),
            "checks": {},
        }
        DEMO_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Controller lifecycle
    # ------------------------------------------------------------------

    def controller_request(
        self,
        method: str,
        path: str,
        payload: Optional[dict[str, Any]] = None,
        timeout: float = 3.0,
    ) -> dict[str, Any]:
        status, response = http_json(
            self.rest_host,
            self.rest_port,
            method,
            path,
            payload,
            timeout,
        )
        if status >= 400:
            raise RuntimeError(
                f"{method} {path} returned HTTP {status}: {response}"
            )
        return response

    def controller_health(self) -> Optional[dict[str, Any]]:
        try:
            status, payload = http_json(
                self.rest_host, self.rest_port, "GET", "/health", timeout=1.0
            )
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if status == 200 and payload.get("service") == "exam-sdn-controller":
            return payload
        return None

    def start_controller(self) -> None:
        if self.controller_health() is not None:
            self.owns_controller = False
            print("Reusing the controller already listening on "
                  f"{self.rest_host}:{self.rest_port}")
            return
        if not CONTROLLER.is_file():
            raise RuntimeError(f"missing controller wrapper: {CONTROLLER}")
        log_path = DEMO_DIR / "controller.log"
        decision_log = DEMO_DIR / "controller.ndjson"
        command = [
            str(CONTROLLER),
            "--mode",
            "static",
            "--of-port",
            str(self.args.of_port),
            "--rest-host",
            self.rest_bind,
            "--rest-port",
            str(self.rest_port),
            "--block-seconds",
            str(self.args.block_seconds),
            "--monitor-interval",
            str(self.args.monitor_interval),
            "--health-interval",
            str(self.args.health_interval),
            "--health-failures",
            str(self.args.health_failures),
            "--log-file",
            str(decision_log),
        ]
        print("Starting controller: " + " ".join(command))
        with open(log_path, "ab") as log_handle:
            self.controller_proc = subprocess.Popen(
                command,
                cwd=str(ROOT),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.owns_controller = True
        try:
            wait_until(
                self.controller_health,
                12.0,
                0.25,
                "controller REST API",
            )
        except TimeoutError:
            self._dump_tail(log_path)
            raise RuntimeError(
                "controller did not start; see " + str(log_path)
            ) from None
        self.artifacts["controller_log"] = str(log_path)
        self.artifacts["controller_decision_log"] = str(decision_log)

    def stop_controller(self) -> None:
        proc = self.controller_proc
        if proc is None or not self.owns_controller:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3.0)
        self.controller_proc = None

    # ------------------------------------------------------------------
    # Topology and helpers
    # ------------------------------------------------------------------

    def start_topology(self) -> None:
        if os.geteuid() != 0:
            raise RuntimeError("the demo must run as root; use sudo")
        sys.path.insert(0, str(ROOT))
        from sdn.topology import build_network  # noqa: PLC0415

        print(
            f"Building topology: students={self.args.students} "
            f"bw={self.args.bw} Mbps"
        )
        self.net, _ = build_network(
            students=self.args.students,
            bw=self.args.bw,
            run_dir=str(RUN_DIR),
        )
        self.artifacts["topology_metadata"] = str(METADATA)

    def state(self) -> dict[str, Any]:
        return self.controller_request("GET", "/sdn/state")

    def stats(self) -> dict[str, Any]:
        return self.controller_request("GET", "/sdn/stats")

    def set_mode(self, mode: str) -> None:
        self.controller_request("POST", "/sdn/mode", {"mode": mode})
        wait_until(
            lambda: self.state().get("mode") == mode,
            5.0,
            0.2,
            f"mode {mode}",
        )

    def wait_connected(self) -> None:
        state = wait_until(
            lambda: (
                current
                if (current := self.state()).get("switches", {}).get(
                    "all_connected"
                )
                else None
            ),
            15.0,
            0.5,
            "all switches connected and metadata loaded",
        )
        self.summary["checks"]["switches"] = state["switches"]

    def host(self, name: str) -> Any:
        if self.net is None:
            raise RuntimeError("topology is not running")
        return self.net.get(name)

    def start_stubs(self) -> None:
        python = sys.executable or "/usr/bin/python3"
        for name, ip in (("srv1", SRV1), ("srv2", SRV2)):
            host = self.host(name)
            log_path = DEMO_DIR / f"stub-{name}.ndjson"
            command = [
                python,
                str(STUB),
                "--host",
                "0.0.0.0",
                "--port",
                str(SERVER_PORT),
                "--server-id",
                f"{ip}:{SERVER_PORT}",
                "--controller-url",
                f"http://10.0.0.254:{self.rest_port}",
                "--active-attempts",
                "0",
                "--heartbeat-s",
                "2.0",
                "--log-file",
                str(log_path),
            ]
            print(f"Starting stub backend on {name} ({ip}:{SERVER_PORT})")
            self.stub_procs[name] = host.popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.artifacts[f"stub_{name}_log"] = str(log_path)
        time.sleep(1.5)

    def start_iperf_server(self) -> None:
        iperf = shutil.which("iperf3")
        if not iperf:
            raise RuntimeError("iperf3 is not installed")
        bg2 = self.host("bg2")
        self.iperf_server = bg2.popen(
            [iperf, "-s", "-p", str(IPERF_PORT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.8)

    # ------------------------------------------------------------------
    # Checks
    # ------------------------------------------------------------------

    def run_pingall(self, label: str) -> float:
        print(f"Running pingall in {label} mode ...")
        loss = self.net.pingAll(timeout="1")
        self.summary["checks"][f"pingall_{label}"] = {
            "loss_percent": loss,
            "ok": loss == 0,
        }
        print(f"  pingall {label}: {loss}% loss")
        if loss != 0:
            raise RuntimeError(f"pingall failed in {label} mode ({loss}% loss)")
        return loss

    def set_exam_active(self, active: bool) -> None:
        payload: dict[str, Any] = {
            "event": "exam_started" if active else "exam_ended",
            "server": f"{SRV1}:{SERVER_PORT}",
            "exam_id": 1,
            "active_attempts": 1 if active else 0,
        }
        self.controller_request("POST", "/exam/event", payload)

    def run_latency_probe(self, label: str) -> dict[str, Any]:
        st1 = self.host("st1")
        out_path = DEMO_DIR / f"probe-{label}.json"
        python = sys.executable or "/usr/bin/python3"
        command = [
            python,
            str(PROBE),
            "--target",
            f"{VIP}:{SERVER_PORT}",
            "--count",
            str(self.args.probe_count),
            "--interval-ms",
            str(self.args.probe_interval_ms),
            "--timeout",
            "2.0",
            "--warmup",
            "3",
            "--out",
            str(out_path),
        ]
        proc = st1.popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=30.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
        lines = [line for line in (stdout or "").splitlines() if line.strip()]
        if not lines:
            raise RuntimeError(
                f"latency probe produced no output (stderr: {stderr!r})"
            )
        try:
            result = json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"latency probe output was not JSON: {lines[-1]!r}"
            ) from exc
        self.artifacts[f"probe_{label}"] = str(out_path)
        if result.get("errors", 0):
            print(
                f"  probe {label}: {result['errors']} errors "
                f"{result.get('error_details')}"
            )
        print(
            f"  probe {label}: median={result['latency_ms']['median']} ms "
            f"p95={result['latency_ms']['p95']} ms "
            f"servers={result.get('servers')}"
        )
        return result

    def measure_mode(self, mode: str) -> dict[str, Any]:
        print(f"\n=== {mode.upper()} benchmark ===")
        self.set_mode(mode)
        if mode == "sdn":
            self.set_exam_active(True)
            wait_until(
                lambda: self.state().get("qos", {}).get("active"),
                10.0,
                0.25,
                "SDN QoS active",
            )
            # QoS becomes active before the asynchronous meter/queue setup
            # finishes.  Wait for a concrete backend so the benchmark really
            # measures the classified flows.
            wait_until(
                lambda: self.state().get("qos", {}).get("backend")
                in ("meter", "queue", "priority"),
                10.0,
                0.25,
                "SDN QoS backend ready",
            )
        iperf = shutil.which("iperf3")
        if not iperf:
            raise RuntimeError("iperf3 is not installed")
        bg1 = self.host("bg1")
        print(
            f"  starting iperf3 background load for "
            f"{self.args.iperf_seconds}s"
        )
        self.iperf_client = bg1.popen(
            [
                iperf,
                "-c",
                BG2,
                "-p",
                str(IPERF_PORT),
                "-t",
                str(self.args.iperf_seconds),
                "-P",
                "4",
                "-l",
                "64K",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(3.0)
        try:
            result = self.run_latency_probe(mode)
        finally:
            if self.iperf_client.poll() is None:
                self.iperf_client.terminate()
            try:
                self.iperf_client.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self.iperf_client.kill()
                self.iperf_client.wait(timeout=3.0)
            self.iperf_client = None
        result["mode"] = mode
        result["qos"] = self.state().get("qos")
        self.summary["checks"][f"benchmark_{mode}"] = result
        return result

    def check_flood_block(self) -> None:
        print("\n=== Abuse detection / blocking ===")
        atk = self.host("atk")
        python = sys.executable or "/usr/bin/python3"
        print("Starting UDP flood from atk to the VIP ...")
        self.flood_proc = atk.popen(
            [
                python,
                str(FLOOD),
                "--target",
                VIP,
                "--port",
                "9",
                "--duration",
                str(self.args.block_seconds + 25),
                "--size",
                "1200",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        def flooder_is_blocked() -> Optional[dict[str, Any]]:
            current = self.state()
            if any(
                item.get("host") == "atk"
                for item in current.get("blocked_hosts", [])
            ):
                return current
            return None

        try:
            blocked_state = wait_until(
                flooder_is_blocked,
                12.0,
                0.5,
                "atk to be blocked",
            )
        except TimeoutError as exc:
            raise RuntimeError(
                "the controller did not block the flooder; check "
                "the decision log and port statistics"
            ) from exc
        entry = next(
            item
            for item in blocked_state["blocked_hosts"]
            if item.get("host") == "atk"
        )
        print(
            f"  BLOCKED atk: reason={entry['reason']} "
            f"rx={entry['rx_bytes_per_s']} B/s "
            f"remaining={entry['remaining_s']}s"
        )
        self.summary["checks"]["block"] = {
            "ok": True,
            "entry": entry,
        }
        self.stop_flood()
        print("  flood stopped; waiting for block timeout ...")
        wait_until(
            lambda: not any(
                item.get("host") == "atk"
                for item in self.state().get("blocked_hosts", [])
            ),
            self.args.block_seconds + 12.0,
            0.5,
            "atk unblock",
        )
        print("  UNBLOCKED atk after timeout")
        self.summary["checks"]["unblock"] = {"ok": True}

    def stop_flood(self) -> None:
        proc = self.flood_proc
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3.0)
        try:
            stdout, _ = proc.communicate(timeout=1.0)
            if stdout:
                print("  flood: " + stdout.strip().splitlines()[-1])
        except (subprocess.TimeoutExpired, ValueError):
            pass
        self.flood_proc = None

    def check_failover(self) -> None:
        print("\n=== VIP failover ===")
        state = self.state()
        if state.get("vip", {}).get("backend") != "srv1":
            raise RuntimeError(
                "VIP is not on srv1 before the failover test: "
                f"{state.get('vip')}"
            )
        print("Stopping srv1 backend ...")
        proc = self.stub_procs.pop("srv1", None)
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3.0)
        srv1 = self.host("srv1")
        srv1.cmd(f"pkill -f {STUB.name} >/dev/null 2>&1 || true")
        state = wait_until(
            lambda: (
                current
                if (current := self.state()).get("vip", {}).get(
                    "backend"
                )
                == "srv2"
                else None
            ),
            self.args.health_interval * self.args.health_failures + 12.0,
            0.5,
            "VIP failover to srv2",
        )
        print(
            f"  VIP moved to {state['vip']['backend']} "
            f"({state['vip']['backend_ip']}); "
            f"failovers={state['vip']['failovers']}"
        )
        probe = self.run_latency_probe("failover")
        servers = probe.get("servers") or {}
        if SRV2 + ":" + str(SERVER_PORT) not in servers:
            raise RuntimeError(
                "probe after failover did not reach srv2: "
                f"{servers}"
            )
        self.summary["checks"]["failover"] = {
            "ok": True,
            "vip": state["vip"],
            "probe": probe,
        }
        print("  FAILOVER OK: new VIP flows reached srv2")

    # ------------------------------------------------------------------
    # Reporting and cleanup
    # ------------------------------------------------------------------

    def save_artifacts(self) -> None:
        try:
            state = self.state()
            stats = self.stats()
            state_path = DEMO_DIR / "state.json"
            stats_path = DEMO_DIR / "stats.json"
            state_path.write_text(
                json.dumps(state, indent=2) + "\n", encoding="utf-8"
            )
            stats_path.write_text(
                json.dumps(stats, indent=2) + "\n", encoding="utf-8"
            )
            self.artifacts["state"] = str(state_path)
            self.artifacts["stats"] = str(stats_path)
            self.summary["final_state"] = {
                "mode": state.get("mode"),
                "exam_active": state.get("exam", {}).get("active"),
                "qos": state.get("qos"),
                "vip": state.get("vip"),
                "blocked_hosts": state.get("blocked_hosts"),
                "all_connected": state.get("switches", {}).get(
                    "all_connected"
                ),
            }
            self.summary["final_stats"] = stats.get("totals")
        except Exception as exc:  # noqa: BLE001 - best effort
            print(f"Could not save final state/stats: {exc}")

    def _dump_tail(self, path: Path, lines: int = 40) -> None:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        print(f"--- {path} (tail) ---", file=sys.stderr)
        for line in content.splitlines()[-lines:]:
            print(line, file=sys.stderr)

    def cleanup(self) -> None:
        self.stop_flood()
        if self.iperf_client is not None and self.iperf_client.poll() is None:
            self.iperf_client.terminate()
        for proc in self.stub_procs.values():
            if proc.poll() is None:
                proc.terminate()
        self.stub_procs.clear()
        if self.iperf_server is not None and self.iperf_server.poll() is None:
            self.iperf_server.terminate()
        self.iperf_server = None
        if self.net is not None:
            print("Stopping Mininet topology ...")
            try:
                self.net.stop()
            except Exception as exc:  # noqa: BLE001
                print(f"topology cleanup warning: {exc}", file=sys.stderr)
            self.net = None
        self.stop_controller()

    def run(self) -> int:
        self.start_controller()
        self.start_topology()
        self.wait_connected()
        self.start_stubs()
        self.start_iperf_server()

        self.set_mode("static")
        self.run_pingall("static")
        static_result = self.measure_mode("static")

        self.set_mode("sdn")
        self.run_pingall("sdn")
        sdn_result = self.measure_mode("sdn")

        static_p95 = static_result["latency_ms"]["p95"]
        sdn_p95 = sdn_result["latency_ms"]["p95"]
        improved = sdn_p95 <= static_p95 and (
            sdn_p95 < static_p95 or static_p95 < 1.0
        )
        self.summary["checks"]["latency_comparison"] = {
            "static_p95_ms": static_p95,
            "sdn_p95_ms": sdn_p95,
            "sdn_better_or_equal": improved,
        }
        print(
            f"\nLatency under iperf: static p95={static_p95} ms, "
            f"SDN p95={sdn_p95} ms "
            f"({'PASS' if improved else 'WARN: not lower'})"
        )

        if not self.args.no_flood:
            self.check_flood_block()
        if not self.args.no_failover:
            self.check_failover()

        self.set_exam_active(False)
        self.save_artifacts()
        self.summary["status"] = (
            "pass" if improved else "pass-with-benchmark-warning"
        )
        print("\n=== Demo complete ===")
        print(json.dumps(self.summary["checks"], indent=2))
        print("\nArtifacts:")
        for name, path in sorted(self.artifacts.items()):
            print(f"  {name}: {path}")
        if not improved:
            print(
                "\nWARNING: SDN p95 was not lower than static.  Check the "
                "iperf load, meter support and controller decision log."
            )
        return 0

    def write_summary(self) -> None:
        self.summary["finished_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
        self.summary["artifacts"] = self.artifacts
        path = DEMO_DIR / "summary.json"
        path.write_text(
            json.dumps(self.summary, indent=2) + "\n", encoding="utf-8"
        )
        self.artifacts["summary"] = str(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="End-to-end SDN demo for the online-exam project"
    )
    parser.add_argument("--students", type=int, default=10)
    parser.add_argument("--bw", type=float, default=10.0)
    parser.add_argument("--of-port", type=int, default=6653)
    parser.add_argument("--rest-host", default="0.0.0.0")
    parser.add_argument("--rest-port", type=int, default=9090)
    parser.add_argument(
        "--monitor-interval", type=float, default=2.0
    )
    parser.add_argument("--block-seconds", type=float, default=10.0)
    parser.add_argument(
        "--iperf-seconds", type=float, default=20.0
    )
    parser.add_argument("--probe-count", type=int, default=80)
    parser.add_argument(
        "--probe-interval-ms", type=float, default=40.0
    )
    parser.add_argument("--health-interval", type=float, default=2.0)
    parser.add_argument("--health-failures", type=int, default=2)
    parser.add_argument("--no-flood", action="store_true")
    parser.add_argument("--no-failover", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.students < 1 or args.students > 50:
        parser.error("--students must be between 1 and 50")
    if args.bw <= 0:
        parser.error("--bw must be positive")
    if args.probe_count < 1:
        parser.error("--probe-count must be >= 1")
    if args.iperf_seconds < 5:
        parser.error("--iperf-seconds must be >= 5")
    if args.monitor_interval < 0.5:
        parser.error("--monitor-interval must be >= 0.5")
    if args.block_seconds < 1:
        parser.error("--block-seconds must be >= 1")
    if args.health_interval < 0.25:
        parser.error("--health-interval must be >= 0.25")
    if args.health_failures < 1:
        parser.error("--health-failures must be >= 1")
    if not VENV_PYTHON.is_file():
        parser.error(
            f"missing controller runtime: {VENV_PYTHON}; run "
            "sdn/scripts/check_env.sh for setup hints"
        )
    if os.geteuid() != 0:
        parser.error(
            "Mininet needs root. Run: sudo python3 sdn/scripts/demo.py"
        )

    demo = Demo(args)
    exit_code = 0
    try:
        exit_code = demo.run()
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        exit_code = 130
    except Exception as exc:  # noqa: BLE001 - present a clean failure
        print(f"\nDEMO FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()
        exit_code = 1
    finally:
        try:
            demo.save_artifacts()
        except Exception:  # noqa: BLE001
            pass
        demo.cleanup()
        try:
            demo.write_summary()
        except OSError as exc:
            print(f"could not write summary: {exc}", file=sys.stderr)
        if demo.summary.get("artifacts"):
            print("\nSummary: " + str(demo.summary["artifacts"]["summary"]))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
