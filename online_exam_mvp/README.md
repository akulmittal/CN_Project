# Online Examination System — MVP

A local learning/demo implementation for the Computer Networks project. It demonstrates a C TCP/HTTP backend, Nginx reverse proxy, SQLite persistence, password hashing with libsodium/Argon2id, opaque server-side sessions, role-based routes, exam scheduling, question creation, student attempts, answer autosave, and basic auto-grading.

> **Important security boundary:** this is a coursework MVP, not a production examination service. The demo currently stores grading keys in the same SQLite database as the web API. For a real deployment, move grading keys and grading into a separate service/database inaccessible to the student-facing backend. Do not expose this service to the public internet without HTTPS, request timeouts, rate limiting, CSRF protections, robust HTTP parsing, audit logging, backups, and security review. The current server handles connections sequentially; concurrency and SDN/Mininet/Ryu integration are future implementation milestones, not implemented features.

## Requirements (Ubuntu/WSL)

```bash
sudo apt update
sudo apt install build-essential pkg-config libsqlite3-dev libsodium-dev libcjson-dev sqlite3 nginx curl
```

## Build and initialize

Run from this project root, because the database path is relative:

```bash
make
sqlite3 database/exam.db < database/schema.sql
./create_user examadmin admin
./create_user student01 student
```

Passwords are prompted without echo and are not saved in source code. Use passwords at least 12 characters long.

If migrating from the earlier project, back up `database/exam.db` before changing schema. This MVP uses `exam_questions`, `exam_attempts`, `student_answers`, and `grading_keys` as separate tables; it does not delete existing data.

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

Terminal 1:

```bash
cd ~/CN_Project   # or the directory where you extracted this project
./exam_server
```

Terminal 2 uses Nginx:

```bash
curl -i http://localhost/backend/health
```

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
2. Harden HTTP parser: socket timeouts, strict headers, content-type validation, body limits, correct status/error handling.
3. Use a per-worker SQLite connection (or a database pool) and add thread/epoll-based concurrent clients. Add stress tests and measure p50/p95 latency.
4. Add CSRF tokens for cookie-authenticated state-changing routes; deploy only behind HTTPS and mark cookies `Secure`.
5. Implement WebSocket exam updates/autosave acknowledgement and a browser question-taking UI.
6. Add server-side clock synchronization (chrony/NTP), audit logs, backup/restore tests, and failure recovery.
7. Add Mininet + Open vSwitch + Ryu controller for SDN traffic monitoring/policy enforcement; benchmark with many clients.

## Project layout

- `src/server.c`: C HTTP/TCP backend and routes
- `src/auth.c`, `include/auth.h`: password hashing, sessions, auth helpers
- `src/create_user.c`: safe local account provisioning utility
- `database/schema.sql`: schema for fresh databases
- `web/index.html`: basic admin/student demo UI
- `config/nginx-exam.conf`: Nginx proxy example
