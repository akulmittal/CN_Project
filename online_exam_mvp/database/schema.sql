PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('student','admin')),
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS exams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    start_time INTEGER NOT NULL,
    duration_minutes INTEGER NOT NULL CHECK(duration_minutes BETWEEN 1 AND 1440),
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','scheduled','active','completed')),
    created_by INTEGER NOT NULL,
    created_at INTEGER NOT NULL DEFAULT(unixepoch()),
    FOREIGN KEY(created_by) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_exams_start_time ON exams(start_time);

CREATE TABLE IF NOT EXISTS exam_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_id INTEGER NOT NULL,
    question_text TEXT NOT NULL,
    question_type TEXT NOT NULL CHECK(question_type IN ('mcq','integer','text')),
    options_json TEXT,
    marks INTEGER NOT NULL DEFAULT 1 CHECK(marks > 0),
    position INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY(exam_id) REFERENCES exams(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_exam_questions_exam_id ON exam_questions(exam_id);

CREATE TABLE IF NOT EXISTS exam_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    started_at INTEGER NOT NULL,
    submitted_at INTEGER,
    status TEXT NOT NULL DEFAULT 'in_progress' CHECK(status IN ('in_progress','submitted','graded')),
    score INTEGER,
    UNIQUE(exam_id,user_id),
    FOREIGN KEY(exam_id) REFERENCES exams(id),
    FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS student_answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id INTEGER NOT NULL,
    question_id INTEGER NOT NULL,
    answer_text TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL DEFAULT(unixepoch()),
    UNIQUE(attempt_id,question_id),
    FOREIGN KEY(attempt_id) REFERENCES exam_attempts(id) ON DELETE CASCADE,
    FOREIGN KEY(question_id) REFERENCES exam_questions(id)
);
CREATE INDEX IF NOT EXISTS idx_answers_attempt ON student_answers(attempt_id);

-- Demo-only grading keys. For the final architecture, move this table to a separate grading service/database.
CREATE TABLE IF NOT EXISTS grading_keys (
    question_id INTEGER PRIMARY KEY,
    answer_text TEXT NOT NULL,
    FOREIGN KEY(question_id) REFERENCES exam_questions(id) ON DELETE CASCADE
);
