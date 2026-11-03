#!/usr/bin/env python3
"""Mininet topology for the SDN exam-system demo.

Layout (10.0.0.0/24)::

    srv1 10.0.0.1:8080 ---+
    srv2 10.0.0.2:8080 ---+--- s_srv ==== 10 Mbps / 2 ms ==== s_stu --- st1..stN
    bg2  10.0.0.202 -----+                                     |        10.0.0.10+i
    root 10.0.0.254 (REST/OpenFlow)                            +--- bg1 10.0.0.201
                                                               +--- atk 10.0.0.250

Both switches speak OpenFlow 1.3 and connect to the controller at
10.0.0.254:6653.  The root namespace is connected to ``s_srv`` with a veth so
the controller REST endpoint is reachable from the simulated hosts.

The script writes ``run/topology.json`` after the switches are up.  The
controller uses that file to map DPIDs, ports, MACs and IPs without having to
guess OVS port numbers.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from mininet.cli import CLI
from mininet.link import Link, TCLink
from mininet.log import error, info, lg
from mininet.net import Mininet
from mininet.node import Host, Node, OVSSwitch, RemoteController

# ---------------------------------------------------------------------------
# Address plan
# ---------------------------------------------------------------------------

SRV1_IP = "10.0.0.1"
SRV2_IP = "10.0.0.2"
VIP_IP = "10.0.0.100"
CONTROLLER_IP = "10.0.0.254"
STUDENT_BASE = 10  # st1 -> 10.0.0.11, ..., stN -> 10.0.0.(10+N)
BG1_IP = "10.0.0.201"
BG2_IP = "10.0.0.202"
ATTACKER_IP = "10.0.0.250"

SRV1_MAC = "00:00:00:00:00:01"
SRV2_MAC = "00:00:00:00:00:02"
SRV_SWITCH = "s_srv"
STU_SWITCH = "s_stu"

# 50 students fit comfortably in the 00:00:00:00:10:xx range.
STUDENT_MAC_PREFIX = "00:00:00:00:10"
BG1_MAC = "00:00:00:00:02:01"
BG2_MAC = "00:00:00:00:02:02"
ATTACKER_MAC = "00:00:00:00:03:01"

DEFAULT_BW_MBPS = 10.0
DEFAULT_DELAY = "2ms"
DEFAULT_CONTROLLER_PORT = 6653
DEFAULT_REST_PORT = 9090
DEFAULT_RUN_DIR = str(Path(__file__).resolve().parents[1] / "run")


def student_ip(index: int) -> str:
    """Return the IPv4 address for student ``index`` (1-based)."""
    return f"10.0.0.{STUDENT_BASE + index}"


def student_mac(index: int) -> str:
    """Return a deterministic MAC for student ``index`` (1-based)."""
    if not 1 <= index <= 50:
        raise ValueError("student index must be between 1 and 50")
    return f"{STUDENT_MAC_PREFIX}:{index:02x}"


def student_name(index: int) -> str:
    return f"st{index}"


# ---------------------------------------------------------------------------
# Mininet helpers
# ---------------------------------------------------------------------------


def _ovs_port_map(switch: OVSSwitch) -> dict[str, int]:
    """Map OVS port/interface name -> OpenFlow port number.

    ``ovs-vsctl get Interface <name> ofport`` is more stable than parsing
    ``ovs-ofctl show`` output, whose formatting changes between OVS versions.
    """
    mapping: dict[str, int] = {}
    output = switch.cmd(f"ovs-vsctl list-ports {switch.name}") or ""
    for port_name in output.split():
        raw = (switch.cmd(f"ovs-vsctl get Interface {port_name} ofport") or "").strip()
        raw = raw.splitlines()[0].strip() if raw else ""
        try:
            port_no = int(raw)
        except ValueError:
            continue
        if port_no >= 0:
            mapping[port_name] = port_no
    return mapping


def _wait_ovs_port_map(
    switch: OVSSwitch, expected: int, timeout: float = 8.0
) -> dict[str, int]:
    """Wait briefly for ovs-vswitchd to assign all port numbers."""
    deadline = time.monotonic() + timeout
    mapping: dict[str, int] = {}
    while time.monotonic() < deadline:
        mapping = _ovs_port_map(switch)
        if len(mapping) >= expected:
            return mapping
        time.sleep(0.2)
    return mapping


def _switch_metadata(
    switch: OVSSwitch,
    port_map: dict[str, int],
    host_ports: dict[str, int],
    bottleneck_port: Optional[int],
    root_port: Optional[int] = None,
) -> dict[str, Any]:
    return {
        "name": switch.name,
        "dpid": switch.dpid,
        "dpid_int": int(switch.dpid, 16),
        "ports": dict(sorted(port_map.items())),
        "host_ports": dict(sorted(host_ports.items())),
        "bottleneck_port": bottleneck_port,
        "root_port": root_port,
    }


def _host_metadata(
    name: str,
    ip: str,
    mac: str,
    switch: str,
    port: Optional[int],
    side: str,
    role: str,
) -> dict[str, Any]:
    return {
        "name": name,
        "ip": ip,
        "mac": mac,
        "switch": switch,
        "port": port,
        "side": side,
        "role": role,
    }


# ---------------------------------------------------------------------------
# Network construction
# ---------------------------------------------------------------------------


def build_network(
    students: int = 10,
    bw: float = DEFAULT_BW_MBPS,
    delay: str = DEFAULT_DELAY,
    controller_ip: str = CONTROLLER_IP,
    controller_port: int = DEFAULT_CONTROLLER_PORT,
    rest_port: int = DEFAULT_REST_PORT,
    run_dir: str = DEFAULT_RUN_DIR,
) -> tuple[Mininet, dict[str, Any]]:
    """Build and start the exam SDN topology.

    Must be called as root (Mininet/OVS requirement).  Returns the running
    ``Mininet`` object and the metadata dictionary also written to
    ``run/topology.json``.
    """
    if os.geteuid() != 0:
        raise RuntimeError("Mininet must run as root; use sudo")
    if not 1 <= students <= 50:
        raise ValueError("--students must be between 1 and 50")
    if bw <= 0:
        raise ValueError("--bw must be positive")

    run_path = Path(run_dir).resolve()
    run_path.mkdir(parents=True, exist_ok=True)
    metadata_path = run_path / "topology.json"

    info("*** Creating switches and hosts\n")
    net = Mininet(
        topo=None,
        build=False,
        controller=None,
        switch=OVSSwitch,
        host=Host,
        link=Link,
        waitConnected=False,
        autoSetMacs=False,
        autoStaticArp=False,
    )
    net.addController(
        "c0", controller=RemoteController, ip=controller_ip, port=controller_port
    )

    # Mininet derives a default DPID from digits in the switch name; these
    # names have none, so pass explicit stable DPIDs (0x1 and 0x2).
    s_srv = net.addSwitch(
        SRV_SWITCH,
        cls=OVSSwitch,
        dpid="0000000000000001",
        protocols="OpenFlow13",
        failMode="secure",
        datapath="kernel",
    )
    s_stu = net.addSwitch(
        STU_SWITCH,
        cls=OVSSwitch,
        dpid="0000000000000002",
        protocols="OpenFlow13",
        failMode="secure",
        datapath="kernel",
    )

    hosts: dict[str, Any] = {}
    host_links: dict[str, Any] = {}

    def add_host(name: str, ip: str, mac: str, switch: OVSSwitch) -> Any:
        host = net.addHost(name, ip=f"{ip}/24", mac=mac)
        link = net.addLink(host, switch, cls=Link)
        hosts[name] = host
        host_links[name] = link
        return host

    srv1 = add_host("srv1", SRV1_IP, SRV1_MAC, s_srv)
    srv2 = add_host("srv2", SRV2_IP, SRV2_MAC, s_srv)
    bg2 = add_host("bg2", BG2_IP, BG2_MAC, s_srv)

    students_meta: list[dict[str, Any]] = []
    for index in range(1, students + 1):
        name = student_name(index)
        ip = student_ip(index)
        mac = student_mac(index)
        add_host(name, ip, mac, s_stu)
        students_meta.append(
            {"name": name, "ip": ip, "mac": mac, "index": index}
        )

    bg1 = add_host("bg1", BG1_IP, BG1_MAC, s_stu)
    atk = add_host("atk", ATTACKER_IP, ATTACKER_MAC, s_stu)

    # Root namespace node; connect it to the server-side switch so controller
    # REST/heartbeat traffic does not consume the student bottleneck.
    info("*** Connecting root namespace to %s\n" % SRV_SWITCH)
    root = Node("root", inNamespace=False)
    root_link = net.addLink(root, s_srv, cls=Link)
    root_intf = root_link.intf1
    root.setIP(f"{controller_ip}/32", intf=root_intf)

    info("*** Adding %.1f Mbps / %s bottleneck\n" % (bw, delay))
    bottleneck = net.addLink(
        s_stu,
        s_srv,
        cls=TCLink,
        bw=bw,
        delay=delay,
        use_htb=True,
        max_queue_size=1000,
    )

    net.start()
    connected = net.waitConnected(timeout=20)
    if not connected:
        info(
            "*** WARNING: not all switches connected to the controller; "
            "start sdn/scripts/run_controller.sh first\n"
        )

    # Root namespace route to the simulated network.  The /32 address on
    # root-eth0 makes 10.0.0.254 local to the controller.
    root.cmd(f"ip link set {root_intf.name} up")
    root.cmd(f"ip route replace 10.0.0.0/24 dev {root_intf.name}")
    root_mac = root_intf.MAC() or root_intf.updateMAC()
    if not root_mac:
        info("*** WARNING: could not read root-eth0 MAC address\n")

    # Static-mode VIP: srv1 owns the VIP on loopback and clients use a static
    # ARP entry for the VIP MAC.  In SDN mode the controller rewrites VIP
    # traffic to whichever backend is healthy, while the client keeps the same
    # ARP entry.  srv1's MAC is deliberately the VIP MAC.
    srv1.cmd(f"ip addr replace {VIP_IP}/32 dev lo")
    for name, host in hosts.items():
        if name in ("srv1", "srv2"):
            continue
        intf = host.defaultIntf()
        host.cmd(
            f"ip neigh replace {VIP_IP} lladdr {SRV1_MAC} dev {intf.name}"
        )

    # Wait for OVS to publish ofport numbers, then build the metadata map.
    info("*** Reading OVS port numbers\n")
    expected_srv = 5  # srv1 srv2 bg2 root bottleneck
    expected_stu = students + 3  # students bg1 atk bottleneck
    srv_ports = _wait_ovs_port_map(s_srv, expected_srv)
    stu_ports = _wait_ovs_port_map(s_stu, expected_stu)

    host_ports_srv: dict[str, int] = {}
    for name in ("srv1", "srv2", "bg2"):
        iface = host_links[name].intf2.name
        if iface in srv_ports:
            host_ports_srv[name] = srv_ports[iface]

    host_ports_stu: dict[str, int] = {}
    for index in range(1, students + 1):
        name = student_name(index)
        iface = host_links[name].intf2.name
        if iface in stu_ports:
            host_ports_stu[name] = stu_ports[iface]
    for name in ("bg1", "atk"):
        iface = host_links[name].intf2.name
        if iface in stu_ports:
            host_ports_stu[name] = stu_ports[iface]

    bottleneck_stu_port = stu_ports.get(bottleneck.intf1.name)
    bottleneck_srv_port = srv_ports.get(bottleneck.intf2.name)
    root_port = srv_ports.get(root_link.intf2.name)

    switches_meta = {
        SRV_SWITCH: _switch_metadata(
            s_srv, srv_ports, host_ports_srv, bottleneck_srv_port, root_port
        ),
        STU_SWITCH: _switch_metadata(
            s_stu, stu_ports, host_ports_stu, bottleneck_stu_port
        ),
    }

    servers_meta: dict[str, Any] = {
        SRV1_IP: _host_metadata(
            "srv1", SRV1_IP, SRV1_MAC, SRV_SWITCH,
            host_ports_srv.get("srv1"), "server", "exam_server",
        ),
        SRV2_IP: _host_metadata(
            "srv2", SRV2_IP, SRV2_MAC, SRV_SWITCH,
            host_ports_srv.get("srv2"), "server", "exam_server",
        ),
    }

    clients_meta: dict[str, Any] = {}
    for item in students_meta:
        clients_meta[item["ip"]] = _host_metadata(
            item["name"], item["ip"], item["mac"], STU_SWITCH,
            host_ports_stu.get(item["name"]), "student", "student",
        )
    clients_meta[BG1_IP] = _host_metadata(
        "bg1", BG1_IP, BG1_MAC, STU_SWITCH,
        host_ports_stu.get("bg1"), "student", "background",
    )
    clients_meta[BG2_IP] = _host_metadata(
        "bg2", BG2_IP, BG2_MAC, SRV_SWITCH,
        host_ports_srv.get("bg2"), "server", "background",
    )
    clients_meta[ATTACKER_IP] = _host_metadata(
        "atk", ATTACKER_IP, ATTACKER_MAC, STU_SWITCH,
        host_ports_stu.get("atk"), "student", "attacker",
    )

    metadata: dict[str, Any] = {
        "generated_at": round(time.time(), 3),
        "students": students,
        "student_base": STUDENT_BASE,
        "vip": VIP_IP,
        "vip_mac": SRV1_MAC,
        "controller": {
            "ip": controller_ip,
            "of_port": controller_port,
            "rest_port": rest_port,
            "rest_url": f"http://{controller_ip}:{rest_port}",
        },
        "root": {
            "name": "root",
            "ip": controller_ip,
            "mac": root_mac,
            "switch": SRV_SWITCH,
            "port": root_port,
            "iface": root_intf.name,
        },
        "server_switch": SRV_SWITCH,
        "student_switch": STU_SWITCH,
        "bottleneck": {
            "bw_mbps": bw,
            "delay": delay,
            "student_iface": bottleneck.intf1.name,
            "server_iface": bottleneck.intf2.name,
            "student_port": bottleneck_stu_port,
            "server_port": bottleneck_srv_port,
        },
        "switches": switches_meta,
        "servers": servers_meta,
        "clients": clients_meta,
        "students_map": {item["ip"]: item for item in students_meta},
        "background": {
            "bg1": {"ip": BG1_IP, "mac": BG1_MAC, "side": "student"},
            "bg2": {"ip": BG2_IP, "mac": BG2_MAC, "side": "server"},
        },
        "attacker": {"ip": ATTACKER_IP, "mac": ATTACKER_MAC, "side": "student"},
    }

    tmp_path = metadata_path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(metadata, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp_path, metadata_path)

    missing = []
    for name, port in (
        ("bottleneck-student", bottleneck_stu_port),
        ("bottleneck-server", bottleneck_srv_port),
        ("root", root_port),
    ):
        if port is None:
            missing.append(name)
    if missing:
        info("*** WARNING: missing OVS port metadata: %s\n" % ", ".join(missing))

    info("*** Topology ready; metadata: %s\n" % metadata_path)
    info(
        "*** VIP %s:%d, controller %s:%d, students=%d, bottleneck=%.1f Mbps/%s\n"
        % (VIP_IP, 8080, controller_ip, controller_port, students, bw, delay)
    )
    return net, metadata


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the exam-system Mininet/OVS topology."
    )
    parser.add_argument(
        "--students", type=int, default=10,
        help="number of student hosts st1..stN (1..50, default 10)",
    )
    parser.add_argument(
        "--bw", type=float, default=DEFAULT_BW_MBPS,
        help="bottleneck bandwidth in Mbps (default 10)",
    )
    parser.add_argument(
        "--delay", default=DEFAULT_DELAY,
        help="bottleneck delay (default 2ms)",
    )
    parser.add_argument(
        "--controller-ip", default=CONTROLLER_IP,
        help="controller/root-namespace IPv4 (default 10.0.0.254)",
    )
    parser.add_argument(
        "--controller-port", type=int, default=DEFAULT_CONTROLLER_PORT,
        help="OpenFlow controller port (default 6653)",
    )
    parser.add_argument(
        "--rest-port", type=int, default=DEFAULT_REST_PORT,
        help="controller REST port advertised in metadata (default 9090)",
    )
    parser.add_argument(
        "--run-dir", default=DEFAULT_RUN_DIR,
        help="directory for topology.json and demo state",
    )
    parser.add_argument(
        "--no-cli", action="store_true",
        help="start the network and wait instead of opening the Mininet CLI",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    lg.setLogLevel("info")

    if os.geteuid() != 0:
        error("This topology must run as root. Try: sudo python3 sdn/topology.py\n")
        return 1

    net: Optional[Mininet] = None
    try:
        net, _metadata = build_network(
            students=args.students,
            bw=args.bw,
            delay=args.delay,
            controller_ip=args.controller_ip,
            controller_port=args.controller_port,
            rest_port=args.rest_port,
            run_dir=args.run_dir,
        )
        if args.no_cli:
            info("*** Running headless; press Ctrl-C to stop\n")
            while True:
                time.sleep(1)
        else:
            CLI(net)
    except KeyboardInterrupt:
        info("\n*** Interrupted\n")
    except Exception as exc:  # noqa: BLE001 - CLI should show a clean error
        error(f"topology: {exc}\n")
        return 1
    finally:
        if net is not None:
            info("*** Stopping network\n")
            try:
                net.stop()
            except Exception as exc:  # noqa: BLE001
                error(f"topology cleanup: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
