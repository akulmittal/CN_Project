# Server design notes (viva reference)

This document explains the choices behind the Mininet-ready exam server. It is
intended to be read alongside `src/server.c`, `src/create_user.c`,
`client/exam_client.py`, and `tests/integration.sh`.

## 1. Scope and constraints

- The HTTP server is implemented with the C socket API (`socket`, `bind`,
  `listen`, `accept`, `recv`, `send`) and pthreads. No third-party HTTP or
  socket library is used.
- Python exam clients use only the standard `socket` module for exam traffic.
  `http.server` appears only in the test stub controller and is not part of the
  exam client path.
- Nginx is optional and is **not** used in the Mininet topology. Students talk
  to a server IP or the SDN VIP directly.
- The server must not be coupled to the controller: Ryu can be stopped, slow, or
  unreachable without affecting API latency or correctness.

## 2. Connection and threading model

The main thread owns the listening socket. For each accepted connection it:

1. atomically increments `active_connections`;
2. configures 15-second receive/send socket timeouts;
3. creates a detached worker thread.

Each worker has its own SQLite connection in thread-local storage:

```c
static __thread Auth auth;
```

This is important because SQLite connections are not safe to share between
threads. The worker initializes the connection, handles exactly one HTTP
request, closes the connection, and decrements both the worker count and the
atomic connection counter. `SIGINT`/`SIGTERM` set a flag; the main loop stops
accepting, closes the listening socket, and waits on a condition variable until
all workers finish. This gives a clean exit and makes an immediate restart on
the same port possible (together with `SO_REUSEADDR`).

Login is deliberately expensive because Argon2id is expensive. A small
semaphore (`LOGIN_CONCURRENCY = 4`) limits concurrent password verification so
a login burst cannot starve every worker. Other requests only pay for SQLite
work.

### Why one request per connection?

The MVP parser supports a single `Content-Length` body and has no HTTP/1.1
pipelining or keep-alive state machine. Every response explicitly sends:

```text
Connection: close
Content-Length: <exact length>
```

and the worker closes the socket after the send. This is documented and
intentional: it keeps the parser small and auditable. The Python clients open a
fresh socket for every operation, so connection setup is included in measured
latency. A production server would use a proper HTTP parser and keep-alive, but
that is outside this coursework's socket-programming scope.

## 3. SDN notifier

### Configuration

- `--controller-url http://HOST:PORT` or `EXAM_CONTROLLER_URL` enables
  notifications. The command-line option takes precedence.
- An empty `--controller-url` disables notifications.
- The URL must use `http://`. The host may be a name or IPv4 address; it is
  resolved once at startup with `getaddrinfo(AF_INET)`.
- The event endpoint is always `/exam/event`.
- `--server-id` is the `server` field in every event, for example
  `10.0.0.1:8080`. If omitted, it defaults to `ADDR:PORT` (or
  `127.0.0.1:PORT` when binding `0.0.0.0`).

### Non-blocking path for client requests

The notifier must never make an exam API call slow or fail. The request path
does only this:

1. build a small JSON object with cJSON;
2. copy it into a fixed-size ring buffer under a mutex;
3. signal a condition variable;
4. return to the HTTP response.

If the ring buffer is full, the oldest event is dropped and the new one is
stored. No client thread ever opens a socket to the controller, waits for the
controller, or returns an error because of it.

### Dedicated thread and heartbeat

One dedicated notifier thread owns all controller I/O. It waits on the queue
with `pthread_cond_timedwait`. Every 2 seconds (or immediately after the
heartbeat deadline passes while events are flowing) it emits:

```json
{
  "event": "server_status",
  "server": "10.0.0.1:8080",
  "exam_id": null,
  "active_attempts": 0,
  "active_connections": 2,
  "ts": 1793638352
}
```

An initial `server_status` is enqueued at startup so the controller sees each
instance promptly.

### Raw HTTP/1.1 POST

The notifier uses raw sockets and a hand-written request:

```http
POST /exam/event HTTP/1.1
Host: 10.0.0.254:9090
Content-Type: application/json
Content-Length: <n>
Connection: close
User-Agent: exam-server-sdn/1

<json>
```

Timeouts are 300 ms for connect, send, and receive:

- the socket is switched to non-blocking mode for `connect`;
- `poll` enforces the connect deadline;
- `SO_SNDTIMEO` / `SO_RCVTIMEO` bound `send` and `recv`;
- the connection is always closed.

Failures are logged (rate-limited to at most one line per 10 seconds so an
offline controller does not flood the console) and otherwise ignored. A 2xx
response is success; a non-2xx or a timeout is a failed delivery. Delivery is
best-effort because this is a demo controller channel, not a transactional
message bus.

### Event semantics

| Event | When | `exam_id` | Counter change |
|---|---|---|---|
| `exam_published` | Publish route updates a draft | exam id | none |
| `attempt_started` | A new `exam_attempts` row is inserted | exam id | `active_attempts += 1` |
| `attempt_submitted` | An in-progress attempt is updated to submitted | exam id | `active_attempts -= 1` (floor 0) |
| `server_status` | Startup and every 2 s | `null` | reports atomics |

`start` is idempotent (`INSERT OR IGNORE`). A resumed attempt does not emit a
second `attempt_started` event or increment the counter. Submit returns an error
if the attempt is not active, so it cannot decrement twice.

### Atomic counters

`active_connections` and `active_attempts` are C11 `atomic_int` values:

- `active_connections` increments on accept and decrements on worker exit.
- `active_attempts` is initialized from
  `SELECT COUNT(*) FROM exam_attempts WHERE status='in_progress'` at startup.
  It is incremented/decremented only for successful local start/submit
  transitions.

With two servers sharing one database, the counters are per-process views. The
shared database remains the source of truth for grading and results. A
production design would maintain a per-server attempt ownership table or ask the
database/controller for a global count; the MVP keeps the hot path free of
extra database queries. The controller's `server_status` events still make
each instance's view visible.

## 4. Health endpoint and response size

`/health` and `/api/v1/health` return JSON:

```json
{"status":"ok","service":"exam-backend","server":"10.0.0.1:8080",
 "active_attempts":3,"active_connections":5}
```

The health request itself is one of those connections, so `active_connections`
includes it while the response is being built.

The old implementation built responses in a 4 KiB stack buffer and rejected
larger JSON. A 50-question paper exceeds that. `respond()` now allocates
exactly the space needed for headers plus body, and `respond_json()` serializes
cJSON dynamically. The admin list and results routes use the same dynamic path,
so they are no longer capped by the old limit. The only remaining bound is the
8 KiB **request** buffer; exam answers and admin question creation fit
comfortably, and the server returns 413 for oversized requests.

## 5. Persistence and two-server operation

`auth_init` opens SQLite with `FULLMUTEX`, enables `foreign_keys`, sets a 5 s
`busy_timeout`, and uses WAL mode. Each worker has its own connection, so
readers do not block each other and writes serialize briefly. The integration
test starts two server processes on different ports with the same database and
logs in through the second server using an account created via the first; this
checks that users, sessions, and exams are shared.

For Mininet:

```bash
# srv1 namespace
./exam_server --addr 0.0.0.0 --port 8080 \
  --db /shared/database/exam.db --server-id 10.0.0.1:8080 \
  --controller-url http://10.0.0.254:9090

# srv2 namespace
./exam_server --addr 0.0.0.0 --port 8080 \
  --db /shared/database/exam.db --server-id 10.0.0.2:8080 \
  --controller-url http://10.0.0.254:9090
```

Binding `0.0.0.0` is necessary because the VIP (`10.0.0.100`) is configured on
each server's loopback for the SDN L2 VIP design. If the server bound only
`10.0.0.1`, a packet whose destination IP is the VIP would not match the
listening socket.

## 6. Account creation and demo seeding

`create_user` supports:

```bash
./create_user [--db PATH] [--password-stdin] USERNAME admin|student
```

- Passwords read from stdin are never passed in `argv`, so they do not appear
  in `ps` or shell history.
- A line longer than the password buffer is rejected rather than truncated.
- Interactive mode still prompts twice with `getpass` and compares the entries.
- The `--db` option lets tests and demos use a temporary database.

`tools/seed_demo.py` generates a 20-character alphanumeric password per
account using `secrets`, invokes `create_user` with the password on stdin,
creates an exam whose start time is in the past, adds 10 questions (or
`--question-count N`), publishes it, and writes `users.csv` with mode `0600`.
The file contains real credentials, is git-ignored, and is never committed.
`--reset-users` deletes the demo usernames first for repeatable local runs.

## 7. Python client and load generator

`client/exam_client.py`:

- opens a new `socket.create_connection` per request;
- writes the HTTP request manually, including `Host`, `Content-Length`,
  `Connection: close`, and the session cookie when known;
- parses the status line, headers, `Set-Cookie`, and JSON body;
- measures latency around connect + send + full response;
- returns an `ExamResponse` with `status`, `ok`, `latency_ms`, `body`, and
  `error`, instead of raising for normal HTTP errors.

`client/loadgen.py`:

- reads `username,password[,role]` from the users file and selects student rows;
- creates one thread per client and synchronizes them with a `Barrier`;
- each client logs in, starts, fetches the paper, saves K answers with optional
  think time, and submits;
- writes `ts_start,client,op,latency_ms,status,ok` with `ok` as `1`/`0`;
- prints one JSON summary line with request/error counts, p50/p95/p99 latency,
  throughput, and wall duration.

Percentiles use the nearest-rank method on successful requests. Throughput is
successful requests divided by wall duration, so login verification cost and
any retries are visible in the numbers. The client never uses `urllib` or
`requests`.

## 8. Tests and acceptance mapping

`tests/integration.sh` covers:

| Requirement | Test |
|---|---|
| build cleanly | `make` with `-Wall -Wextra -Wpedantic` |
| controller integration | `tests/stub_controller.py` receives all four event types |
| 50-question response | seed with `--question-count 50`, loadgen fetches the paper without errors |
| loadgen contract | 20 clients, CSV header, summary JSON, zero errors |
| graceful shutdown/restart | SIGINT, wait for exit, restart on the same port |
| shared WAL database | two servers, login through the second after seeding through the first |
| notifier isolation | server with `http://127.0.0.1:1`, `/health` remains sub-second |

The test does not require Mininet, Ryu, or root privileges.

## 9. Known limitations and honest viva answers

- The notifier queue is lossy under sustained controller failure; that is a
  deliberate trade-off to protect exam latency. A real system would use a local
  durable outbox or a message queue.
- `active_attempts` is a per-process counter. With a shared database and
  cross-server failover it can be temporarily approximate. The database is
  authoritative for grading.
- The HTTP parser is intentionally minimal: one request per connection, no
  chunked bodies, no keep-alive, no TLS. Nginx or a production HTTP stack would
  normally front it.
- The session cookie is not `Secure` because the Mininet demo uses plain HTTP;
  CSRF protection is not implemented.
- Event delivery is best-effort; the controller state is not transactionally
  consistent with SQLite.
- The load generator includes login/Argon2id in its latency summary. For pure
  exam-operation comparisons, pre-create sessions or report login separately in
  the evaluation chapter.
