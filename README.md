# CN Project — Online Examination System with SDN

A Computer Networks coursework project demonstrating a full-stack online examination system with Software-Defined Networking (SDN) integration for VIP failover, QoS prioritization, and attack mitigation.

---

## Project Overview

This project implements a complete online examination platform with:

- **C Backend**: High-performance HTTP/1.1 server with raw sockets, SQLite WAL, Argon2id password hashing
- **Web Frontend**: Single-page application (vanilla JS) for admin/student workflows
- **SDN Controller**: Ryu/os-ken application with OpenFlow 1.3 for traffic engineering
- **Mininet Topology**: Realistic network emulation with VIP, background traffic, and attacker
- **Automated Demo**: End-to-end test suite validating failover, QoS, and security features

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              CLIENT LAYER                                    │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌──────────────────┐   │
│  │   Admin     │  │  Student    │  │   Admin     │  │   Load Generator │   │
│  │   Browser   │  │   Browser   │  │   CLI/curl  │  │   (loadgen.py)   │   │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └────────┬─────────┘   │
└─────────┼────────────────┼────────────────┼──────────────────┼─────────────┘
          │                │                │                  │
          ▼                ▼                ▼                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           NGINX REVERSE PROXY                                │
│                    (Frontend + /backend/ API proxy)                          │
└────────────────────────────────────┬────────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                            C BACKEND (exam_server)                           │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────────┐   │
│  │ HTTP/1.1     │ │ Thread Pool  │ │ SQLite WAL   │ │ SDN Notifier     │   │
│  │ Parser       │ │ (per conn)   │ │ (exam.db)    │ │ (HTTP/1.1 client)│   │
│  └──────────────┘ └──────────────┘ └──────────────┘ └────────┬─────────┘   │
└───────────────────────────────────────────────────────────────┼────────────┘
                                                                │
                    ┌───────────────────────────────────────────┼───────────┐
                    ▼                                           ▼           ▼
        ┌─────────────────────┐ ┌─────────────────────┐ ┌─────────────────────┐
        │      SRV1           │ │      SRV2           │ │   SDN CONTROLLER    │
        │  10.0.0.1:8080      │ │  10.0.0.2:8080      │ │   (os-ken/Ryu)      │
        │  exam_server        │ │  exam_server        │ │  REST: 9090         │
        │  --server-id=...    │ │  --server-id=...    │ │  OF: 6653           │
        └─────────────────────┘ └─────────────────────┘ └─────────────────────┘
                    │                                           │
                    └───────────────┬───────────────────────────┘
                                    ▼
                    ┌─────────────────────────────────────┐
                    │      MININET TOPOLOGY               │
                    │  ┌─────────────────────────────┐    │
                    │  │ s_srv ── VIP (10.0.0.100)   │    │
                    │  │   │           │             │    │
                    │  │ SRV1        SRV2            │    │
                    │  │ 10.0.0.1    10.0.0.2        │    │
                    │  └─────────────────────────────┘    │
                    │  ┌─────────────────────────────┐    │
                    │  │ s_stu ── ST1..STN           │    │
                    │  │   │    BG1  BG2  ATK        │    │
                    │  │   │    10.0.0.201 .202 .250 │    │
                    │  └─────────────────────────────┘    │
                    │  Controller: 10.0.0.254             │
                    └─────────────────────────────────────┘
```

---

## Repository Structure

```
CN_Project/
├── online_exam_mvp/           # C Backend + Frontend
│   ├── src/
│   │   ├── server.c           # HTTP server, routing, API endpoints
│   │   ├── auth.c             # Argon2id, sessions, password hashing
│   │   └── create_user.c      # CLI user provisioning
│   ├── include/auth.h
│   ├── web/index.html         # SPA frontend (admin + student)
│   ├── tools/seed_demo.py     # Demo data generator
│   ├── client/
│   │   ├── exam_client.py     # stdlib HTTP client
│   │   └── loadgen.py         # Concurrent load generator
│   ├── tests/integration.sh   # End-to-end test suite
│   ├── database/schema.sql    # SQLite schema
│   ├── config/nginx-exam.conf # Nginx proxy config
│   └── Makefile
│
├── sdn/                       # SDN Controller + Topology
│   ├── controller/exam_controller.py  # os-ken Ryu app
│   ├── topology.py            # Mininet topology builder
│   ├── stub_server.py         # Lightweight test backend
│   ├── scripts/
│   │   ├── demo.py            # Full automated SDN demo
│   │   ├── run_controller.sh  # Controller launcher
│   │   ├── run_topology.sh    # Mininet launcher
│   │   ├── latency_probe.py   # HTTP latency measurement
│   │   └── flood.py           # UDP flood generator
│   └── requirements.txt       # os-ken, eventlet, dnspython
│
├── docs/
│   ├── sdn-design.md          # SDN architecture & design
│   ├── server-design.md       # Backend threading & notifier
│   └── verification-report.md # Test results
│
├── run/                       # Runtime artifacts (git-ignored)
│   ├── demo/                  # Demo output (logs, stats, state)
│   └── topology.json          # Mininet metadata for controller
│
├── nginx-exam.conf            # Nginx site config (root level)
├── setup_nginx.sh             # One-command nginx setup
├── run_demo.sh                # Interactive demo runner
├── tmux_demo.sh               # Tmux session for demo
└── README.md                  # This file
```

---

## Features

### Backend (C)
- **HTTP/1.1 from scratch** — Raw socket parsing, no external HTTP library
- **Thread-per-connection** — Detached pthreads, each with own SQLite connection
- **SQLite WAL mode** — Concurrent readers/writers, crash-safe
- **Argon2id password hashing** — libsodium, 12+ char minimum
- **Opaque session cookies** — HttpOnly, SameSite=Lax, 24h expiry
- **Role-based access** — Admin / Student routes
- **Exam lifecycle** — Draft → Scheduled → Active → Completed
- **Auto-grading** — MCQ & integer questions via SQL join
- **Answer autosave** — Per-question POST, debounced in frontend
- **SDN Notifier** — Background thread, bounded queue, 300ms timeouts

### Frontend (Vanilla JS)
- **Single-page application** — No build step, no framework
- **Admin dashboard** — Create exams, add questions, publish, view results
- **Student dashboard** — List exams, take exam, timer, question palette
- **Per-question review** — Post-submission answer vs correct answer
- **Responsive design** — Mobile-friendly, dark/light theme

### SDN Controller (os-ken/Ryu)
- **OpenFlow 1.3** — Flow mods, group tables, meters
- **VIP Failover** — Health probes (2s interval), 2 failures = failover
- **QoS** — OpenFlow meters (preferred) → HTB queue fallback
- **Exam-aware policy** — `exam_published` / `attempt_started` events activate QoS
- **Abuse detection** — Packet-in rate limiting, auto-block + timeout
- **REST API** — `/sdn/state`, `/sdn/stats`, `/exam/event`, `/health`

### Mininet Topology
- **2 switches** — `s_srv` (servers), `s_stu` (students)
- **VIP** — 10.0.0.100 (ARP proxy + flow rules)
- **Hosts** — SRV1, SRV2, ST1..STN, BG1, BG2, ATK, Controller
- **Links** — 10 Mbps / 2ms delay (configurable)
- **Background traffic** — iperf3 between BG1/BG2

---

## Quick Start

### Prerequisites (Ubuntu/WSL)
```bash
sudo apt update
sudo apt install build-essential pkg-config libsqlite3-dev \
  libsodium-dev libcjson-dev sqlite3 curl nginx mininet \
  openvswitch-switch iperf3 python3-venv
```

### 1. Build Backend
```bash
cd online_exam_mvp
make
```

### 2. Initialize Database & Users
```bash
sqlite3 database/exam.db < database/schema.sql
read -rsp 'Admin password: ' PW; echo
printf '%s\n' "$PW" | ./create_user --password-stdin examadmin admin
unset PW
read -rsp 'Student password: ' PW; echo
printf '%s\n' "$PW" | ./create_user --password-stdin student01 student
unset PW
```

### 3. Setup Nginx (Frontend + Proxy)
```bash
cd ..
sudo ./setup_nginx.sh
```

### 4. Start Backend
```bash
cd online_exam_mvp
./exam_server --port 8080 --addr 0.0.0.0 --db database/exam.db &
python3 tools/seed_demo.py --students 20 --question-count 10 --reset-users
```

### 5. Access Frontend
Open `http://localhost` in browser. Login with credentials from `users.csv`.

---

## SDN Demo (Full Stack)

### Terminal 1: Controller
```bash
cd /home/akul@Lenovoi7/CN_Project
./run_demo.sh sdn-venv
./run_demo.sh controller
```

### Terminal 2: Mininet
```bash
cd /home/akul@Lenovoi7/CN_Project
./run_demo.sh mininet
# In Mininet CLI:
mininet> srv1 /home/akul@Lenovoi7/CN_Project/online_exam_mvp/exam_server --addr 0.0.0.0 --port 8080 --db /home/akul@Lenovoi7/CN_Project/online_exam_mvp/database/exam.db --server-id 10.0.0.1:8080 --controller-url http://10.0.0.254:9090 &
mininet> srv2 /home/akul@Lenovoi7/CN_Project/online_exam_mvp/exam_server --addr 0.0.0.0 --port 8080 --db /home/akul@Lenovoi7/CN_Project/online_exam_mvp/database/exam.db --server-id 10.0.0.2:8080 --controller-url http://10.0.0.254:9090 &
```

### Terminal 3: Run Automated Demo
```bash
cd /home/akul@Lenovoi7/CN_Project
sudo ./run_demo.sh demo
# Or for specific tests:
sudo python3 sdn/scripts/demo.py --students 10 --no-flood --no-failover
```

### What the Demo Validates
| Test | Description |
|------|-------------|
| Pingall (Static/SDN) | Full connectivity in both modes |
| Latency Benchmark | Static vs SDN mode under iperf background load |
| Flood Blocking | UDP flood from ATK → blocked at switch |
| VIP Failover | Kill SRV1 → VIP moves to SRV2 in ~10s |

---

## API Reference

### Public
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |
| GET | `/api/v1/health` | Health check (alt) |
| POST | `/api/v1/login` | Login → sets session cookie |

### Authenticated (Cookie Required)
| Method | Endpoint | Role | Description |
|--------|----------|------|-------------|
| GET | `/api/v1/me` | Any | Current user info |
| POST | `/api/v1/logout` | Any | Revoke session |

### Admin Only
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/admin/dashboard` | Role check |
| POST | `/api/v1/admin/exams` | Create draft exam |
| GET | `/api/v1/admin/exams` | List all exams |
| PUT | `/api/v1/admin/exams/{id}/schedule` | Edit draft schedule |
| POST | `/api/v1/admin/exams/{id}/publish` | Publish draft |
| POST | `/api/v1/admin/questions` | Add question to draft |
| GET | `/api/v1/admin/results` | All attempts with scores |

### Student Only
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/student/dashboard` | Role check |
| GET | `/api/v1/student/exams` | Published exams |
| POST | `/api/v1/student/exams/{id}/start` | Start attempt |
| GET | `/api/v1/student/exams/{id}/questions` | Paper + saved answers |
| POST | `/api/v1/student/answers` | Save answer |
| POST | `/api/v1/student/exams/{id}/submit` | Submit & grade |
| GET | `/api/v1/student/results` | Own attempt history |
| GET | `/api/v1/student/exams/{id}/result` | Detailed review |

### SDN Controller
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Controller health |
| GET | `/sdn/state` | VIP, QoS, backends, blocked hosts |
| GET | `/sdn/stats` | Flow/packet/byte counters |
| POST | `/exam/event` | Backend event ingestion |
| POST | `/sdn/mode` | Switch static/sdn mode |

---

## Key Design Decisions

| Area | Decision | Rationale |
|------|----------|-----------|
| **C Backend** | Raw sockets, no libevent/libuv | Learning exercise, full control |
| **Threading** | 1 thread/connection, detached | Simplicity, no pool complexity |
| **SQLite** | WAL mode, per-thread connection | Concurrent readers, no locking |
| **Auth** | Argon2id + opaque sessions | Modern hashing, no JWT complexity |
| **HTTP** | Connection: close, no keep-alive | Simpler parsing, stateless |
| **SDN Notifier** | Dedicated thread + bounded queue | Non-blocking, backpressure |
| **QoS** | Meters → HTB fallback | Hardware support varies |
| **Failover** | Health probes (2s/2 failures) | Fast detection, minimal flapping |
| **Frontend** | Vanilla JS, no framework | Zero dependencies, portable |

---

## Security Notes

> **This is a coursework MVP, not production-ready.**

- Grading keys stored in same DB as web API (should be separate service)
- No HTTPS/TLS termination (add certbot + nginx SSL)
- No rate limiting (add nginx `limit_req_zone`)
- No CSRF tokens (add for cookie-based state changes)
- No request body size limits (add nginx `client_max_body_size`)
- No audit logging / backup strategy

---

## Contributors

- **Shankar Nag** — Backend, Frontend, SDN Integration, Demo Automation
- **Akul** — SDN Controller, Mininet Topology, QoS Implementation
- **Akshay Javli** — Documentation, Testing, Demo Scripts

---

## License

Coursework project for Computer Networks. Not licensed for production use.

---

## Acknowledgments

- **os-ken / Ryu** — SDN controller framework
- **Mininet** — Network emulation
- **libsodium** — Argon2id password hashing
- **cJSON** — JSON parsing in C
- **SQLite** — Embedded database with WAL mode