# Online Examination System — MVP

A local learning/demo implementation for the Computer Networks project. It demonstrates a C TCP/HTTP backend using raw sockets, SQLite persistence, password hashing with libsodium/Argon2id, opaque server-side sessions, role-based routes, exam scheduling, question creation, student attempts, answer autosave, and basic auto-grading. The server is also Mininet-ready: it can emit exam/health events to the Ryu controller over a hand-written HTTP/1.1 notifier without blocking client requests.

> **Important security boundary:** this is a coursework MVP, not a production examination service. The demo currently stores grading keys in the same SQLite database as the web API. For a real deployment, move grading keys and grading into a separate service/database inaccessible to the student-facing backend. Do not expose this service to the public internet without HTTPS, request timeouts, rate limiting, CSRF protections, robust HTTP parsing, audit logging, backups, and security review. The backend uses one detached worker thread per connection, each with its own SQLite connection, and shuts down cleanly on SIGINT/SIGTERM.

## Requirements (Ubuntu/WSL)

```bash
sudo apt update
sudo apt install build-essential pkg-config libsqlite3-dev libsodium-dev libcjson-dev sqlite3 curl
```

For the optional browser UI through Nginx, install `nginx` as well. Nginx is **not** used in the Mininet/SDN topology; clients talk directly to the C server or the SDN VIP.

Python tooling uses only the standard library (`socket`, `csv`, `json`, `threading`, `subprocess`); no `requests`/`urllib` is used for exam traffic.

## Build and initialize

Run from this project root, because the default database path is relative:

```bash
make
sqlite3 database/exam.db < database/schema.sql
read -rsp 'Admin password: ' PW; echo
printf '%s\n' "$PW" | ./create_user --password-stdin examadmin admin
unset PW
read -rsp 'Student password: ' PW; echo
printf '%s\n' "$PW" | ./create_user --password-stdin student01 student
unset PW
```

`create_user` reads the password from stdin with `--password-stdin`, so it never appears in shell history or the process list. Interactive use still prompts twice without echo:

```bash
./create_user examadmin admin
```

Passwords must be at least 12 characters. For a repeatable demo with generated credentials, run the server and then:

```bash
python3 tools/seed_demo.py --students 20 --users-file users.csv
```

It creates one admin, 20 students, a published exam with 10 questions, writes `users.csv` (mode 0600, git-ignored), and prints the exam id as JSON. Use `--question-count 50` to exercise a large paper response, and `--reset-users` to recreate the demo accounts in an existing database.

If migrating from the earlier project, back up `database/exam.db` before changing schema. This MVP uses `exam_questions`, `exam_attempts`, `student_answers`, and `grading_keys` as separate tables; it does not delete existing data.

Never commit `users.csv`, cookie jars, or `*.db` files; all are ignored by `.gitignore`.

## Nginx

Your existing setup should already proxy `/backend/` to `127.0.0.1:8080`. If not, add the `location /backend/` block from `config/nginx-exam.conf` inside your Nginx `server {}` block, then run:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

Copy the UI into the Nginx document root if desired:

```bash
sudo cp web/index.html /var/www/html/index.html
```

## Start backend

```bash
./exam_server [--port PORT] [--db PATH] [--addr ADDRESS]
              [--server-id ID] [--controller-url URL]
```

Defaults are port `8080`, database `database/exam.db`, and bind address `0.0.0.0` (all IPv4 interfaces). Run from the project root unless you pass an absolute `--db` path. For example, two independent instances can run without rebuilding:

```bash
./exam_server --port 8080 --db database/exam.db
./exam_server --port 8081 --db database/exam-2.db
```

The server handles each connection in its own detached worker thread and waits for active workers on Ctrl-C/SIGTERM before exiting, so it can be restarted immediately.

### Health counters

`GET /health` and `GET /api/v1/health` return the same JSON. `active_attempts` is an atomic counter initialized from in-progress attempts in the shared database; `active_connections` is the number of accepted connections currently being served (the health request itself is included while it is being handled).

```json
{"status":"ok","service":"exam-backend","server":"10.0.0.1:8080","active_attempts":3,"active_connections":5}
```

### Controller notifier

Set `--controller-url` or the `EXAM_CONTROLLER_URL` environment variable (CLI wins):

```bash
./exam_server --addr 0.0.0.0 --port 8080 \
  --db /absolute/path/database/exam.db \
  --server-id 10.0.0.1:8080 \
  --controller-url http://10.0.0.254:9090
```

The notifier appends events to a bounded in-memory queue and sends them from one dedicated thread using hand-written HTTP/1.1 with 300 ms connect/send/recv timeouts. A slow or unreachable controller never delays or fails an API request. Events are `exam_published`, `attempt_started`, `attempt_submitted`, and a `server_status` heartbeat every 2 seconds:

```json
{"event":"attempt_started","server":"10.0.0.1:8080","exam_id":1,"active_attempts":1,"active_connections":4,"ts":1793638352}
```

Bind servers to `0.0.0.0` in Mininet so they also accept traffic addressed to the VIP whose IP is configured on loopback. Use `--server-id 10.0.0.1:8080` (or `10.0.0.2:8080`) so events identify the correct backend.

### HTTP connection behaviour

The MVP deliberately supports one request per TCP connection. Every response includes `Connection: close`, a correct `Content-Length`, and the worker closes the socket after replying. HTTP keep-alive/pipelining is therefore not supported; clients should open a new connection for each request. `client/exam_client.py` and `client/loadgen.py` follow this contract. Responses are built dynamically, so a 50-question paper is not truncated by the old 4 KiB response buffer.

With the server running, test through Nginx (optional) or directly:

```bash
curl -i http://localhost:8080/health
curl -i http://localhost/backend/health   # if Nginx is configured
```

## Load generation

`client/exam_client.py` is a stdlib-`socket` HTTP client used by the demo tools. `client/loadgen.py` follows the project contract:

```bash
python3 client/loadgen.py \
  --target 127.0.0.1:8080 --clients 20 --exam-id 1 \
  --users-file users.csv --answers-per-client 3 --think-ms 5 \
  --out results/baseline.csv
```

CSV columns are `ts_start,client,op,latency_ms,status,ok`, where `ok` is `1`/`0`. The final stdout line is a summary JSON such as:

```json
{"clients":20,"requests":140,"errors":0,"p50_ms":0.851,"p95_ms":280.794,"p99_ms":351.859,"throughput_rps":385.948,"duration_s":0.363}
```

Each client logs in, starts the exam, fetches the paper, saves `K` answers with the requested think time, and submits. Percentiles and throughput count successful requests only; login latency includes Argon2id verification, so it dominates the tail for large bursts. Diagnostics go to stderr so the summary line stays machine-readable.

## Integration test

```bash
make test          # builds, then runs tests/integration.sh
bash tests/integration.sh
```

The test builds the server, starts a stub controller, seeds 20 students and a 50-question exam on a temporary database, runs 20 concurrent loadgen clients, asserts zero errors and that all four event types arrive, sends SIGINT and restarts immediately on the same port, verifies two servers sharing one WAL database, and checks that `/health` stays fast when the controller URL is unreachable.

## Mininet/SDN notes

The controller REST contract and VIP-to-backend mapping are implemented by the Ryu app in `controller/` and the topology in `mininet/` (added in the SDN milestones). This server-side task only requires:

- bind `0.0.0.0` on each server so VIP traffic is accepted;
- pass the correct `--server-id` and `--db` (a shared WAL database for srv1/srv2);
- pass `--controller-url http://10.0.0.254:9090`;
- do not use Nginx inside Mininet.

See `docs/server-design.md` for the notifier/threading design and viva notes.

## Full demo flow

### 1. Login as admin

```bash
curl -i -c cookies.txt -X POST http://localhost/backend/api/v1/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"examadmin","password":"YOUR_ADMIN_PASSWORD"}'
```

### 2. Create a draft exam

`start_time` is a Unix timestamp in seconds. Pick a future time; the included web UI automatically converts a local date/time.

```bash
curl -i -b cookies.txt -X POST http://localhost/backend/api/v1/admin/exams \
  -H 'Content-Type: application/json' \
  -d '{"title":"Computer Networks Test","start_time":1792000000,"duration_minutes":60}'
```

### 3. List admin exams

```bash
curl -i -b cookies.txt http://localhost/backend/api/v1/admin/exams
```

### 4. Add a question to a draft

Replace `exam_id` as needed. The `correct_answer` field is for demo grading only; it must never be returned to students.

```bash
curl -i -b cookies.txt -X POST http://localhost/backend/api/v1/admin/questions \
  -H 'Content-Type: application/json' \
  -d '{"exam_id":1,"question_text":"What does TCP stand for?","question_type":"mcq","options":["Transmission Control Protocol","Transfer Control Program"],"marks":1,"correct_answer":"Transmission Control Protocol"}'
```

### 5. Publish exam

```bash
curl -i -b cookies.txt -X POST http://localhost/backend/api/v1/admin/exams/1/publish
```

A published exam is `scheduled` if its start time is in the future, or `active` if its time window has begun. The MVP does not yet run a scheduler to automatically change status to `completed`; access checks still enforce the end timestamp.

### 6. Student login and list exams

```bash
curl -i -c student-cookies.txt -X POST http://localhost/backend/api/v1/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"student01","password":"YOUR_STUDENT_PASSWORD"}'
curl -i -b student-cookies.txt http://localhost/backend/api/v1/student/exams
```

### 7. Start, autosave, submit

Only during the exam time window:

```bash
curl -i -b student-cookies.txt -X POST http://localhost/backend/api/v1/student/exams/1/start
curl -i -b student-cookies.txt http://localhost/backend/api/v1/student/exams/1/questions
curl -i -b student-cookies.txt -X POST http://localhost/backend/api/v1/student/answers \
  -H 'Content-Type: application/json' \
  -d '{"question_id":1,"answer":"Transmission Control Protocol"}'
curl -i -b student-cookies.txt -X POST http://localhost/backend/api/v1/student/exams/1/submit
```

### 8. Admin results

```bash
curl -i -b cookies.txt http://localhost/backend/api/v1/admin/results
```

## API summary

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/health` | Public | Health check |
| POST | `/api/v1/login` | Public | Login and set session cookie |
| GET | `/api/v1/me` | Signed in | Current user |
| POST | `/api/v1/logout` | Signed in | Revoke session |
| GET | `/api/v1/admin/dashboard` | Admin | Admin role check |
| GET | `/api/v1/student/dashboard` | Student | Student role check |
| POST | `/api/v1/admin/exams` | Admin | Create draft |
| GET | `/api/v1/admin/exams` | Admin | List exams |
| POST | `/api/v1/admin/exams/:id/publish` | Admin | Publish draft |
| POST | `/api/v1/admin/questions` | Admin | Add question/key to draft |
| GET | `/api/v1/student/exams` | Student | List published exams |
| POST | `/api/v1/student/exams/:id/start` | Student | Start/resume attempt during time window |
| GET | `/api/v1/student/exams/:id/questions` | Student | Get paper and saved answers during active attempt |
| POST | `/api/v1/student/answers` | Student | Save answer |
| POST | `/api/v1/student/exams/:id/submit` | Student | Submit and grade MCQ/integer answers |
| GET | `/api/v1/admin/results` | Admin | List attempts/results |

## Next engineering milestones

1. Move grading keys into a separate grading service/database and use authenticated service-to-service requests.
2. Harden HTTP parser further: strict headers, content-type validation, body limits, request rate limiting, correct status/error handling.
3. Add a database pool or connection reuse for high connection churn; benchmark many clients.
4. Add CSRF tokens for cookie-authenticated state-changing routes; deploy only behind HTTPS and mark cookies `Secure`.
5. Implement WebSocket exam updates/autosave acknowledgement and a browser question-taking UI.
6. Add server-side clock synchronization (chrony/NTP), audit logs, backup/restore tests, and failure recovery.
7. Complete Mininet + Open vSwitch + Ryu controller for SDN VIP failover, exam-aware policy, and attack mitigation.

## Project layout

- `src/server.c`: C HTTP/TCP backend, routes, dynamic responses, SDN notifier
- `src/auth.c`, `include/auth.h`: password hashing, sessions, auth helpers
- `src/create_user.c`: safe local account provisioning (`--password-stdin`)
- `tools/seed_demo.py`: generated demo users + published exam, writes git-ignored `users.csv`
- `client/exam_client.py`: stdlib-`socket` exam HTTP client
- `client/loadgen.py`: concurrent load generator and CSV/summary output
- `tests/integration.sh`: repeatable end-to-end server checks
- `tests/stub_controller.py`: local `/exam/event` receiver used by tests
- `database/schema.sql`: schema for fresh databases
- `web/index.html`: basic admin/student demo UI
- `config/nginx-exam.conf`: optional Nginx proxy example (not used in Mininet)
- `docs/server-design.md`: threading, notifier, response and testing design notes
