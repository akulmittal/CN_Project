#define _GNU_SOURCE

#include "auth.h"
#include <arpa/inet.h>
#include <cjson/cJSON.h>
#include <errno.h>
#include <getopt.h>
#include <limits.h>
#include <netinet/in.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

#define DEFAULT_PORT 8080
#define DEFAULT_DB "database/exam.db"
#define DEFAULT_ADDR "0.0.0.0"
#define REQUEST_SIZE 8192
#define RESPONSE_SIZE 4096
#define SESSION_COOKIE "exam_session"
#define LOGIN_CONCURRENCY 4
#define CLIENT_TIMEOUT_SECONDS 15

/* Each worker owns its SQLite connection. */
static __thread Auth auth;

static const char *database_path = DEFAULT_DB;
static volatile sig_atomic_t shutdown_requested = 0;

static pthread_mutex_t workers_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t workers_cond = PTHREAD_COND_INITIALIZER;
static unsigned int active_workers = 0;

typedef struct {
    pthread_mutex_t mutex;
    pthread_cond_t cond;
    unsigned int available;
} LoginSemaphore;

static LoginSemaphore login_sem = {
    PTHREAD_MUTEX_INITIALIZER,
    PTHREAD_COND_INITIALIZER,
    LOGIN_CONCURRENCY
};

typedef enum {
    API_OK = 0,
    API_ERR_INVALID_FIELDS,
    API_ERR_INVALID_CREDENTIALS,
    API_ERR_EXAM_NOT_AVAILABLE_NOW,
    API_ERR_ATTEMPT_NOT_ACTIVE,
    API_ERR_ANSWER_NOT_SAVED,
    API_ERR_DATABASE,
    API_ERR_GRADING,
    API_ERR_SUBMIT_FAILED,
    API_ERR_OUT_OF_MEMORY
} ApiError;

typedef struct {
    int port;
    const char *db_path;
    const char *addr;
} ServerConfig;

static void login_semaphore_wait(LoginSemaphore *sem)
{
    pthread_mutex_lock(&sem->mutex);
    while (sem->available == 0)
        pthread_cond_wait(&sem->cond, &sem->mutex);
    sem->available--;
    pthread_mutex_unlock(&sem->mutex);
}

static void login_semaphore_post(LoginSemaphore *sem)
{
    pthread_mutex_lock(&sem->mutex);
    sem->available++;
    pthread_cond_signal(&sem->cond);
    pthread_mutex_unlock(&sem->mutex);
}

static int send_all(int fd, const char *data, size_t length)
{
    size_t sent = 0;
    while (sent < length) {
        ssize_t n = send(fd, data + sent, length - sent, 0);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return -1;
        sent += (size_t)n;
    }
    return 0;
}

static void respond(int fd, int status, const char *reason,
                    const char *body, const char *extra_headers)
{
    char response[RESPONSE_SIZE];
    int n = snprintf(response, sizeof(response),
        "HTTP/1.1 %d %s\r\n"
        "Content-Type: application/json; charset=utf-8\r\n"
        "Content-Length: %zu\r\n"
        "Cache-Control: no-store\r\n"
        "X-Content-Type-Options: nosniff\r\n"
        "Connection: close\r\n"
        "%s"
        "\r\n"
        "%s",
        status, reason, strlen(body), extra_headers ? extra_headers : "", body);
    if (n > 0 && (size_t)n < sizeof(response)) send_all(fd, response, (size_t)n);
}

static void respond_json(int fd, int status, const char *reason,
                         cJSON *json, const char *extra_headers)
{
    char *printed = cJSON_PrintUnformatted(json);
    if (!printed) {
        respond(fd, 500, "Internal Server Error",
                "{\"error\":\"response_serialization_failed\"}\n", NULL);
        return;
    }
    if (strlen(printed) >= RESPONSE_SIZE - 512) {
        cJSON_free(printed);
        respond(fd, 500, "Internal Server Error",
                "{\"error\":\"response_too_large\"}\n", NULL);
        return;
    }
    char body[RESPONSE_SIZE];
    snprintf(body, sizeof(body), "%s\n", printed);
    respond(fd, status, reason, body, extra_headers);
    cJSON_free(printed);
}

static void respond_api_error(int fd, ApiError error)
{
    switch (error) {
    case API_ERR_INVALID_FIELDS:
        respond(fd, 400, "Bad Request", "{\"error\":\"invalid_fields\"}\n", NULL);
        break;
    case API_ERR_INVALID_CREDENTIALS:
        respond(fd, 401, "Unauthorized", "{\"error\":\"invalid_credentials\"}\n", NULL);
        break;
    case API_ERR_EXAM_NOT_AVAILABLE_NOW:
        respond(fd, 403, "Forbidden", "{\"error\":\"exam_not_available_now\"}\n", NULL);
        break;
    case API_ERR_ATTEMPT_NOT_ACTIVE:
        respond(fd, 403, "Forbidden", "{\"error\":\"attempt_not_active\"}\n", NULL);
        break;
    case API_ERR_ANSWER_NOT_SAVED:
        respond(fd, 403, "Forbidden",
                "{\"error\":\"answer_not_saved_exam_inactive_or_question_invalid\"}\n", NULL);
        break;
    case API_ERR_GRADING:
        respond(fd, 500, "Internal Server Error", "{\"error\":\"grading_error\"}\n", NULL);
        break;
    case API_ERR_SUBMIT_FAILED:
        respond(fd, 500, "Internal Server Error", "{\"error\":\"submit_failed\"}\n", NULL);
        break;
    case API_ERR_OUT_OF_MEMORY:
        respond(fd, 500, "Internal Server Error", "{\"error\":\"out_of_memory\"}\n", NULL);
        break;
    case API_ERR_DATABASE:
    default:
        respond(fd, 500, "Internal Server Error", "{\"error\":\"database_error\"}\n", NULL);
        break;
    }
}

/* Reads one HTTP request. This intentionally supports only Content-Length bodies. */
static int read_request(int fd, char *buffer, size_t capacity,
                        size_t *header_length, size_t *body_length)
{
    size_t used = 0;
    *header_length = 0;
    *body_length = 0;

    while (used + 1 < capacity) {
        ssize_t n = recv(fd, buffer + used, capacity - used - 1, 0);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return -1;
        used += (size_t)n;
        buffer[used] = '\0';

        char *end = strstr(buffer, "\r\n\r\n");
        if (!end) continue;
        *header_length = (size_t)(end - buffer) + 4;

        char *line = strstr(buffer, "\r\n");
        if (!line) return -1;
        char *cursor = line + 2;
        int found_length = 0;

        while (cursor < end) {
            char *next = strstr(cursor, "\r\n");
            if (!next) break;
            size_t line_len = (size_t)(next - cursor);

            if (line_len >= 15 && strncasecmp(cursor, "Content-Length:", 15) == 0) {
                if (found_length) return -1;
                char *value = cursor + 15;
                while (value < next && (*value == ' ' || *value == '\t')) value++;
                if (value == next || *value == '-') return -1;
                char *tail = NULL;
                errno = 0;
                unsigned long parsed = strtoul(value, &tail, 10);
                while (tail < next && (*tail == ' ' || *tail == '\t')) tail++;
                if (errno || tail != next || parsed >= capacity) return -1;
                *body_length = (size_t)parsed;
                found_length = 1;
            }
            if (line_len >= 18 && strncasecmp(cursor, "Transfer-Encoding:", 18) == 0)
                return -1;
            cursor = next + 2;
        }

        if (*header_length + *body_length >= capacity) return -2;
        while (used < *header_length + *body_length) {
            n = recv(fd, buffer + used, capacity - used - 1, 0);
            if (n < 0 && errno == EINTR) continue;
            if (n <= 0) return -1;
            used += (size_t)n;
            buffer[used] = '\0';
        }
        return 0;
    }
    return -2;
}

static int parse_request_line(char *request, char *method, size_t method_size,
                              char *path, size_t path_size)
{
    char version[16];
    char parsed_method[16];
    char parsed_path[256];
    if (sscanf(request, "%15s %255s %15s", parsed_method, parsed_path, version) != 3)
        return -1;
    if (strncmp(version, "HTTP/1.", 7) != 0) return -1;
    if (strlen(parsed_method) >= method_size || strlen(parsed_path) >= path_size)
        return -1;
    strcpy(method, parsed_method);
    strcpy(path, parsed_path);
    return 0;
}

static int get_session_cookie(const char *request, char *token, size_t token_size)
{
    const char *cursor = strstr(request, "\r\n");
    if (!cursor) return -1;
    cursor += 2;

    while (*cursor) {
        const char *end = strstr(cursor, "\r\n");
        if (!end || end == cursor) break;
        if ((size_t)(end - cursor) >= 7 && strncasecmp(cursor, "Cookie:", 7) == 0) {
            const char *p = cursor + 7;
            while (p < end) {
                while (p < end && (*p == ' ' || *p == '\t' || *p == ';')) p++;
                const char *name = p;
                while (p < end && *p != '=' && *p != ';') p++;
                if (p < end && *p == '=') {
                    size_t name_len = (size_t)(p - name);
                    p++;
                    const char *value = p;
                    while (p < end && *p != ';') p++;
                    if (name_len == strlen(SESSION_COOKIE) &&
                        strncmp(name, SESSION_COOKIE, name_len) == 0) {
                        size_t value_len = (size_t)(p - value);
                        if (value_len + 1 > token_size) return -1;
                        memcpy(token, value, value_len);
                        token[value_len] = '\0';
                        return 0;
                    }
                }
            }
        }
        cursor = end + 2;
    }
    return -1;
}

static int create_exam(const char *title, long long start_time,
                       int duration_minutes, int admin_id, int *exam_id)
{
    sqlite3_stmt *stmt = NULL;
    if (!title || title[0] == '\0' || strlen(title) > 200 ||
        start_time <= 0 || duration_minutes <= 0 || duration_minutes > 1440 ||
        admin_id <= 0 || !exam_id) return 0;

    const char *sql =
        "INSERT INTO exams (title, start_time, duration_minutes, status, created_by) "
        "VALUES (?, ?, ?, 'draft', ?);";
    if (sqlite3_prepare_v2(auth.db, sql, -1, &stmt, NULL) != SQLITE_OK) return 0;

    sqlite3_bind_text(stmt, 1, title, -1, SQLITE_TRANSIENT);
    sqlite3_bind_int64(stmt, 2, (sqlite3_int64)start_time);
    sqlite3_bind_int(stmt, 3, duration_minutes);
    sqlite3_bind_int(stmt, 4, admin_id);
    int result = sqlite3_step(stmt);
    if (result == SQLITE_DONE) {
        *exam_id = (int)sqlite3_last_insert_rowid(auth.db);
        sqlite3_finalize(stmt);
        return 1;
    }
    sqlite3_finalize(stmt);
    return 0;
}

static cJSON *api_login(const char *username, const char *password,
                        char *token, size_t token_size, ApiError *error)
{
    if (!username || !password || !token || token_size < 65 || !error) {
        if (error) *error = API_ERR_INVALID_FIELDS;
        return NULL;
    }

    AuthUser user = {0};
    login_semaphore_wait(&login_sem);
    int result = auth_login(&auth, username, password, token, token_size, &user);
    login_semaphore_post(&login_sem);
    if (result != 0) {
        *error = API_ERR_INVALID_CREDENTIALS;
        return NULL;
    }

    cJSON *response = cJSON_CreateObject();
    cJSON *user_json = cJSON_CreateObject();
    if (!response || !user_json) {
        if (response) cJSON_Delete(response);
        if (user_json) cJSON_Delete(user_json);
        *error = API_ERR_OUT_OF_MEMORY;
        return NULL;
    }

    cJSON_AddStringToObject(response, "status", "ok");
    cJSON_AddNumberToObject(user_json, "id", user.user_id);
    cJSON_AddStringToObject(user_json, "username", user.username);
    cJSON_AddStringToObject(user_json, "role", user.role);
    cJSON_AddItemToObject(response, "user", user_json);
    return response;
}

static cJSON *api_start_exam(int exam_id, int user_id, ApiError *error)
{
    if (exam_id <= 0 || user_id <= 0 || !error) {
        if (error) *error = API_ERR_INVALID_FIELDS;
        return NULL;
    }

    sqlite3_stmt *stmt = NULL;
    const char *sql =
        "INSERT OR IGNORE INTO exam_attempts(exam_id,user_id,started_at) "
        "SELECT id,?,? FROM exams WHERE id=? AND status IN ('scheduled','active') "
        "AND unixepoch()>=start_time AND unixepoch()<start_time+duration_minutes*60";
    if (sqlite3_prepare_v2(auth.db, sql, -1, &stmt, NULL) != SQLITE_OK) {
        *error = API_ERR_DATABASE;
        return NULL;
    }
    sqlite3_bind_int(stmt, 1, user_id);
    sqlite3_bind_int64(stmt, 2, (sqlite3_int64)time(NULL));
    sqlite3_bind_int(stmt, 3, exam_id);
    int rc = sqlite3_step(stmt);
    sqlite3_finalize(stmt);
    if (rc != SQLITE_DONE) {
        *error = API_ERR_DATABASE;
        return NULL;
    }

    if (sqlite3_prepare_v2(auth.db,
            "SELECT id,status FROM exam_attempts WHERE exam_id=? AND user_id=?",
            -1, &stmt, NULL) != SQLITE_OK) {
        *error = API_ERR_DATABASE;
        return NULL;
    }
    sqlite3_bind_int(stmt, 1, exam_id);
    sqlite3_bind_int(stmt, 2, user_id);
    rc = sqlite3_step(stmt);
    if (rc == SQLITE_ROW) {
        int attempt_id = sqlite3_column_int(stmt, 0);
        const char *status = (const char *)sqlite3_column_text(stmt, 1);
        cJSON *response = cJSON_CreateObject();
        if (response) {
            cJSON_AddNumberToObject(response, "attempt_id", attempt_id);
            cJSON_AddStringToObject(response, "status", status ? status : "");
        }
        sqlite3_finalize(stmt);
        if (!response) {
            *error = API_ERR_OUT_OF_MEMORY;
            return NULL;
        }
        return response;
    }
    sqlite3_finalize(stmt);
    *error = (rc == SQLITE_DONE) ? API_ERR_EXAM_NOT_AVAILABLE_NOW : API_ERR_DATABASE;
    return NULL;
}

static cJSON *api_get_questions(int exam_id, int user_id, ApiError *error)
{
    if (exam_id <= 0 || user_id <= 0 || !error) {
        if (error) *error = API_ERR_INVALID_FIELDS;
        return NULL;
    }

    sqlite3_stmt *stmt = NULL;
    const char *check =
        "SELECT a.id FROM exam_attempts a JOIN exams e ON e.id=a.exam_id "
        "WHERE a.exam_id=? AND a.user_id=? AND a.status='in_progress' "
        "AND unixepoch()>=e.start_time AND unixepoch()<e.start_time+e.duration_minutes*60";
    if (sqlite3_prepare_v2(auth.db, check, -1, &stmt, NULL) != SQLITE_OK) {
        *error = API_ERR_DATABASE;
        return NULL;
    }
    sqlite3_bind_int(stmt, 1, exam_id);
    sqlite3_bind_int(stmt, 2, user_id);
    int rc = sqlite3_step(stmt);
    if (rc != SQLITE_ROW) {
        sqlite3_finalize(stmt);
        *error = (rc == SQLITE_DONE) ? API_ERR_ATTEMPT_NOT_ACTIVE : API_ERR_DATABASE;
        return NULL;
    }
    int attempt_id = sqlite3_column_int(stmt, 0);
    sqlite3_finalize(stmt);

    const char *sql =
        "SELECT q.id,q.question_text,q.question_type,q.options_json,q.marks,"
        "COALESCE(a.answer_text,'') FROM exam_questions q "
        "LEFT JOIN student_answers a ON a.question_id=q.id AND a.attempt_id=? "
        "WHERE q.exam_id=? ORDER BY q.position,q.id";
    if (sqlite3_prepare_v2(auth.db, sql, -1, &stmt, NULL) != SQLITE_OK) {
        *error = API_ERR_DATABASE;
        return NULL;
    }
    sqlite3_bind_int(stmt, 1, attempt_id);
    sqlite3_bind_int(stmt, 2, exam_id);

    cJSON *root = cJSON_CreateObject();
    cJSON *questions = cJSON_CreateArray();
    if (!root || !questions) {
        if (root) cJSON_Delete(root);
        if (questions) cJSON_Delete(questions);
        sqlite3_finalize(stmt);
        *error = API_ERR_OUT_OF_MEMORY;
        return NULL;
    }

    while (sqlite3_step(stmt) == SQLITE_ROW) {
        cJSON *question = cJSON_CreateObject();
        if (!question) {
            cJSON_Delete(questions);
            cJSON_Delete(root);
            sqlite3_finalize(stmt);
            *error = API_ERR_OUT_OF_MEMORY;
            return NULL;
        }
        cJSON_AddNumberToObject(question, "id", sqlite3_column_int(stmt, 0));
        cJSON_AddStringToObject(question, "question_text",
                                (const char *)sqlite3_column_text(stmt, 1));
        cJSON_AddStringToObject(question, "question_type",
                                (const char *)sqlite3_column_text(stmt, 2));
        const char *options = (const char *)sqlite3_column_text(stmt, 3);
        if (options) {
            cJSON *options_json = cJSON_Parse(options);
            if (options_json) cJSON_AddItemToObject(question, "options", options_json);
        }
        cJSON_AddNumberToObject(question, "marks", sqlite3_column_int(stmt, 4));
        cJSON_AddStringToObject(question, "answer",
                                (const char *)sqlite3_column_text(stmt, 5));
        cJSON_AddItemToArray(questions, question);
    }
    sqlite3_finalize(stmt);

    cJSON_AddNumberToObject(root, "exam_id", exam_id);
    cJSON_AddItemToObject(root, "questions", questions);
    return root;
}

static cJSON *api_save_answer(int user_id, int question_id,
                              const char *answer_text, ApiError *error)
{
    if (user_id <= 0 || question_id <= 0 || !answer_text ||
        strlen(answer_text) > 4000 || !error) {
        if (error) *error = API_ERR_INVALID_FIELDS;
        return NULL;
    }

    sqlite3_stmt *stmt = NULL;
    const char *sql =
        "INSERT INTO student_answers(attempt_id,question_id,answer_text,updated_at) "
        "SELECT a.id,q.id,?,? FROM exam_attempts a JOIN exams e ON e.id=a.exam_id "
        "JOIN exam_questions q ON q.exam_id=e.id WHERE a.user_id=? "
        "AND a.status='in_progress' AND q.id=? "
        "AND unixepoch()<e.start_time+e.duration_minutes*60 "
        "ON CONFLICT(attempt_id,question_id) DO UPDATE SET "
        "answer_text=excluded.answer_text,updated_at=excluded.updated_at";
    if (sqlite3_prepare_v2(auth.db, sql, -1, &stmt, NULL) != SQLITE_OK) {
        *error = API_ERR_DATABASE;
        return NULL;
    }
    sqlite3_bind_text(stmt, 1, answer_text, -1, SQLITE_TRANSIENT);
    sqlite3_bind_int64(stmt, 2, (sqlite3_int64)time(NULL));
    sqlite3_bind_int(stmt, 3, user_id);
    sqlite3_bind_int(stmt, 4, question_id);
    int rc = sqlite3_step(stmt);
    int changed = sqlite3_changes(auth.db);
    sqlite3_finalize(stmt);
    if (rc != SQLITE_DONE || !changed) {
        *error = API_ERR_ANSWER_NOT_SAVED;
        return NULL;
    }

    cJSON *response = cJSON_CreateObject();
    if (!response) {
        *error = API_ERR_OUT_OF_MEMORY;
        return NULL;
    }
    cJSON_AddStringToObject(response, "status", "saved");
    return response;
}

static cJSON *api_submit_exam(int exam_id, int user_id, ApiError *error)
{
    if (exam_id <= 0 || user_id <= 0 || !error) {
        if (error) *error = API_ERR_INVALID_FIELDS;
        return NULL;
    }

    sqlite3_stmt *stmt = NULL;
    const char *sql =
        "SELECT a.id FROM exam_attempts a JOIN exams e ON e.id=a.exam_id "
        "WHERE a.exam_id=? AND a.user_id=? AND a.status='in_progress' "
        "AND unixepoch()>=e.start_time AND unixepoch()<e.start_time+e.duration_minutes*60";
    if (sqlite3_prepare_v2(auth.db, sql, -1, &stmt, NULL) != SQLITE_OK) {
        *error = API_ERR_DATABASE;
        return NULL;
    }
    sqlite3_bind_int(stmt, 1, exam_id);
    sqlite3_bind_int(stmt, 2, user_id);
    int rc = sqlite3_step(stmt);
    if (rc != SQLITE_ROW) {
        sqlite3_finalize(stmt);
        *error = (rc == SQLITE_DONE) ? API_ERR_ATTEMPT_NOT_ACTIVE : API_ERR_DATABASE;
        return NULL;
    }
    int attempt_id = sqlite3_column_int(stmt, 0);
    sqlite3_finalize(stmt);

    const char *grade =
        "SELECT COALESCE(SUM(CASE WHEN lower(trim(ans.answer_text))=lower(trim(k.answer_text)) "
        "THEN q.marks ELSE 0 END),0) FROM exam_questions q "
        "JOIN grading_keys k ON k.question_id=q.id "
        "LEFT JOIN student_answers ans ON ans.question_id=q.id AND ans.attempt_id=? "
        "WHERE q.exam_id=? AND q.question_type IN ('mcq','integer')";
    if (sqlite3_prepare_v2(auth.db, grade, -1, &stmt, NULL) != SQLITE_OK) {
        *error = API_ERR_GRADING;
        return NULL;
    }
    sqlite3_bind_int(stmt, 1, attempt_id);
    sqlite3_bind_int(stmt, 2, exam_id);
    rc = sqlite3_step(stmt);
    int score = rc == SQLITE_ROW ? sqlite3_column_int(stmt, 0) : 0;
    sqlite3_finalize(stmt);
    if (rc != SQLITE_ROW && rc != SQLITE_DONE) {
        *error = API_ERR_GRADING;
        return NULL;
    }

    if (sqlite3_prepare_v2(auth.db,
            "UPDATE exam_attempts SET submitted_at=?,status='submitted',score=? "
            "WHERE id=? AND status='in_progress'",
            -1, &stmt, NULL) != SQLITE_OK) {
        *error = API_ERR_DATABASE;
        return NULL;
    }
    sqlite3_bind_int64(stmt, 1, (sqlite3_int64)time(NULL));
    sqlite3_bind_int(stmt, 2, score);
    sqlite3_bind_int(stmt, 3, attempt_id);
    rc = sqlite3_step(stmt);
    sqlite3_finalize(stmt);
    if (rc != SQLITE_DONE) {
        *error = API_ERR_SUBMIT_FAILED;
        return NULL;
    }

    cJSON *response = cJSON_CreateObject();
    if (!response) {
        *error = API_ERR_OUT_OF_MEMORY;
        return NULL;
    }
    cJSON_AddStringToObject(response, "status", "submitted");
    cJSON_AddNumberToObject(response, "score", score);
    return response;
}

static void handle_request(int fd)
{
    char request[REQUEST_SIZE];
    size_t header_length = 0, body_length = 0;
    int read_result = read_request(fd, request, sizeof(request), &header_length, &body_length);
    if (read_result == -2) {
        respond(fd, 413, "Content Too Large", "{\"error\":\"request_too_large\"}\n", NULL);
        return;
    }
    if (read_result != 0) {
        respond(fd, 400, "Bad Request", "{\"error\":\"invalid_request\"}\n", NULL);
        return;
    }

    char method[16], path[256];
    if (parse_request_line(request, method, sizeof(method), path, sizeof(path)) != 0) {
        respond(fd, 400, "Bad Request", "{\"error\":\"malformed_request_line\"}\n", NULL);
        return;
    }
    char *query = strchr(path, '?');
    if (query) *query = '\0';
    printf("%s %s\n", method, path);
    fflush(stdout);

    if (strcmp(method, "GET") == 0 &&
        (strcmp(path, "/health") == 0 || strcmp(path, "/api/v1/health") == 0)) {
        respond(fd, 200, "OK", "{\"status\":\"ok\",\"service\":\"exam-backend\"}\n", NULL);
        return;
    }

    if (strcmp(path, "/api/v1/login") == 0) {
        if (strcmp(method, "POST") != 0) {
            respond(fd, 405, "Method Not Allowed", "{\"error\":\"method_not_allowed\"}\n", "Allow: POST\r\n");
            return;
        }
        if (body_length == 0) {
            respond(fd, 400, "Bad Request", "{\"error\":\"json_body_required\"}\n", NULL);
            return;
        }
        char *body = request + header_length;
        body[body_length] = '\0';
        cJSON *json = cJSON_Parse(body);
        cJSON *username_json = json ? cJSON_GetObjectItemCaseSensitive(json, "username") : NULL;
        cJSON *password_json = json ? cJSON_GetObjectItemCaseSensitive(json, "password") : NULL;
        if (!cJSON_IsString(username_json) || !cJSON_IsString(password_json) ||
            !username_json->valuestring || !password_json->valuestring) {
            if (json) cJSON_Delete(json);
            respond(fd, 400, "Bad Request", "{\"error\":\"username_and_password_required\"}\n", NULL);
            return;
        }
        char token[65] = {0};
        ApiError error = API_OK;
        cJSON *response = api_login(username_json->valuestring,
                                    password_json->valuestring,
                                    token, sizeof(token), &error);
        cJSON_Delete(json);
        if (!response) {
            respond_api_error(fd, error);
            return;
        }
        char headers[256];
        snprintf(headers, sizeof(headers),
                 "Set-Cookie: %s=%s; Path=/backend/; HttpOnly; SameSite=Lax\r\n",
                 SESSION_COOKIE, token);
        respond_json(fd, 200, "OK", response, headers);
        cJSON_Delete(response);
        return;
    }

    if (strcmp(path, "/api/v1/me") == 0) {
        if (strcmp(method, "GET") != 0) {
            respond(fd, 405, "Method Not Allowed", "{\"error\":\"method_not_allowed\"}\n", "Allow: GET\r\n");
            return;
        }
        char token[65]; AuthUser user;
        if (get_session_cookie(request, token, sizeof(token)) != 0 ||
            auth_validate_session(&auth, token, &user) != 0) {
            respond(fd, 401, "Unauthorized", "{\"error\":\"authentication_required\"}\n", NULL);
            return;
        }
        cJSON *j = cJSON_CreateObject();
        cJSON_AddNumberToObject(j, "id", user.user_id);
        cJSON_AddStringToObject(j, "username", user.username);
        cJSON_AddStringToObject(j, "role", user.role);
        char *printed = cJSON_PrintUnformatted(j);
        char body[512]; snprintf(body, sizeof(body), "%s\n", printed ? printed : "{}");
        respond(fd, 200, "OK", body, NULL);
        if (printed) cJSON_free(printed);
        cJSON_Delete(j);
        return;
    }

    if (strcmp(path, "/api/v1/logout") == 0) {
        if (strcmp(method, "POST") != 0) {
            respond(fd, 405, "Method Not Allowed", "{\"error\":\"method_not_allowed\"}\n", "Allow: POST\r\n");
            return;
        }
        char token[65];
        if (get_session_cookie(request, token, sizeof(token)) != 0 || auth_logout(&auth, token) != 0) {
            respond(fd, 401, "Unauthorized", "{\"error\":\"authentication_required\"}\n", NULL);
            return;
        }
        respond(fd, 200, "OK", "{\"status\":\"logged_out\"}\n",
                "Set-Cookie: exam_session=; Path=/backend/; HttpOnly; SameSite=Lax; Max-Age=0\r\n");
        return;
    }

    if (strcmp(path, "/api/v1/admin/dashboard") == 0 ||
        strcmp(path, "/api/v1/student/dashboard") == 0) {
        if (strcmp(method, "GET") != 0) {
            respond(fd, 405, "Method Not Allowed", "{\"error\":\"method_not_allowed\"}\n", "Allow: GET\r\n");
            return;
        }
        char token[65]; AuthUser user;
        if (get_session_cookie(request, token, sizeof(token)) != 0 ||
            auth_validate_session(&auth, token, &user) != 0) {
            respond(fd, 401, "Unauthorized", "{\"error\":\"authentication_required\"}\n", NULL);
            return;
        }
        const int admin_route = strcmp(path, "/api/v1/admin/dashboard") == 0;
        const char *required_role = admin_route ? "admin" : "student";
        if (strcmp(user.role, required_role) != 0) {
            respond(fd, 403, "Forbidden", admin_route ? "{\"error\":\"admin_role_required\"}\n" : "{\"error\":\"student_role_required\"}\n", NULL);
            return;
        }
        respond(fd, 200, "OK", admin_route ? "{\"status\":\"ok\",\"dashboard\":\"admin\"}\n" : "{\"status\":\"ok\",\"dashboard\":\"student\"}\n", NULL);
        return;
    }

    if (strcmp(path, "/api/v1/admin/exams") == 0 &&
    strcmp(method, "POST") == 0) {
        if (strcmp(method, "POST") != 0) {
            respond(fd, 405, "Method Not Allowed", "{\"error\":\"method_not_allowed\"}\n", "Allow: POST\r\n");
            return;
        }
        char token[65]; AuthUser user;
        if (get_session_cookie(request, token, sizeof(token)) != 0 ||
            auth_validate_session(&auth, token, &user) != 0) {
            respond(fd, 401, "Unauthorized", "{\"error\":\"authentication_required\"}\n", NULL);
            return;
        }
        if (strcmp(user.role, "admin") != 0) {
            respond(fd, 403, "Forbidden", "{\"error\":\"admin_role_required\"}\n", NULL);
            return;
        }
        if (body_length == 0) {
            respond(fd, 400, "Bad Request", "{\"error\":\"json_body_required\"}\n", NULL);
            return;
        }
        char *body = request + header_length;
        body[body_length] = '\0';
        cJSON *json = cJSON_Parse(body);
        cJSON *title_json = json ? cJSON_GetObjectItemCaseSensitive(json, "title") : NULL;
        cJSON *start_json = json ? cJSON_GetObjectItemCaseSensitive(json, "start_time") : NULL;
        cJSON *duration_json = json ? cJSON_GetObjectItemCaseSensitive(json, "duration_minutes") : NULL;
        int valid = cJSON_IsString(title_json) && title_json->valuestring &&
                    cJSON_IsNumber(start_json) && cJSON_IsNumber(duration_json) &&
                    start_json->valuedouble > 0 &&
                    start_json->valuedouble <= 9007199254740991.0 &&
                    start_json->valuedouble == (double)(long long)start_json->valuedouble &&
                    duration_json->valuedouble >= 1 && duration_json->valuedouble <= 1440 &&
                    duration_json->valuedouble == (double)(int)duration_json->valuedouble;
        if (!valid) {
            if (json) cJSON_Delete(json);
            respond(fd, 400, "Bad Request", "{\"error\":\"invalid_fields\"}\n", NULL);
            return;
        }
        int exam_id = 0;
        int created = create_exam(title_json->valuestring,
                                  (long long)start_json->valuedouble,
                                  (int)duration_json->valuedouble,
                                  user.user_id, &exam_id);
        cJSON_Delete(json);
        if (!created) {
            respond(fd, 400, "Bad Request", "{\"error\":\"exam_creation_failed\"}\n", NULL);
            return;
        }
        char response_body[128];
        snprintf(response_body, sizeof(response_body),
                 "{\"status\":\"created\",\"exam_id\":%d}\n", exam_id);
        respond(fd, 201, "Created", response_body, NULL);
        return;
    }


    /* Admin: list all exams, including drafts. */
    if (strcmp(path, "/api/v1/admin/exams") == 0 && strcmp(method, "GET") == 0) {
        char token[65]; AuthUser user;
        if (get_session_cookie(request, token, sizeof(token)) != 0 || auth_validate_session(&auth, token, &user) != 0) { respond(fd,401,"Unauthorized","{\"error\":\"authentication_required\"}\n",NULL); return; }
        if (strcmp(user.role,"admin") != 0) { respond(fd,403,"Forbidden","{\"error\":\"admin_role_required\"}\n",NULL); return; }
        sqlite3_stmt *st=NULL; cJSON *root=cJSON_CreateObject(), *arr=cJSON_CreateArray();
        if (!root || !arr || sqlite3_prepare_v2(auth.db,"SELECT id,title,start_time,duration_minutes,status,created_by FROM exams ORDER BY id DESC LIMIT 100",-1,&st,NULL)!=SQLITE_OK) { if(root)cJSON_Delete(root); if(arr)cJSON_Delete(arr); respond(fd,500,"Internal Server Error","{\"error\":\"database_error\"}\n",NULL); return; }
        while(sqlite3_step(st)==SQLITE_ROW){ cJSON *e=cJSON_CreateObject(); cJSON_AddNumberToObject(e,"id",sqlite3_column_int(st,0)); cJSON_AddStringToObject(e,"title",(const char*)sqlite3_column_text(st,1)); cJSON_AddNumberToObject(e,"start_time",(double)sqlite3_column_int64(st,2)); cJSON_AddNumberToObject(e,"duration_minutes",sqlite3_column_int(st,3)); cJSON_AddStringToObject(e,"status",(const char*)sqlite3_column_text(st,4)); cJSON_AddNumberToObject(e,"created_by",sqlite3_column_int(st,5)); cJSON_AddItemToArray(arr,e); }
        sqlite3_finalize(st); cJSON_AddItemToObject(root,"exams",arr); char *p=cJSON_PrintUnformatted(root); if(p && strlen(p)<RESPONSE_SIZE-512){ char b[RESPONSE_SIZE];snprintf(b,sizeof(b),"%s",p);respond(fd,200,"OK",b,NULL);}else respond(fd,500,"Internal Server Error","{\"error\":\"response_too_large\"}\n",NULL); if(p)cJSON_free(p);cJSON_Delete(root);return;
    }

    /* Students can see published exams, but never drafts. */
    if (strcmp(path, "/api/v1/student/exams") == 0 && strcmp(method, "GET") == 0) {
        char token[65]; AuthUser user;
        if (get_session_cookie(request, token, sizeof(token)) != 0 || auth_validate_session(&auth, token, &user) != 0) { respond(fd,401,"Unauthorized","{\"error\":\"authentication_required\"}\n",NULL); return; }
        if (strcmp(user.role,"student") != 0) { respond(fd,403,"Forbidden","{\"error\":\"student_role_required\"}\n",NULL); return; }
        sqlite3_stmt *st=NULL; cJSON *root=cJSON_CreateObject(), *arr=cJSON_CreateArray();
        const char *sql="SELECT id,title,start_time,duration_minutes,status FROM exams WHERE status IN ('scheduled','active','completed') ORDER BY start_time LIMIT 100";
        if(!root||!arr||sqlite3_prepare_v2(auth.db,sql,-1,&st,NULL)!=SQLITE_OK){if(root)cJSON_Delete(root);if(arr)cJSON_Delete(arr);respond(fd,500,"Internal Server Error","{\"error\":\"database_error\"}\n",NULL);return;}
        while(sqlite3_step(st)==SQLITE_ROW){cJSON *e=cJSON_CreateObject();cJSON_AddNumberToObject(e,"id",sqlite3_column_int(st,0));cJSON_AddStringToObject(e,"title",(const char*)sqlite3_column_text(st,1));cJSON_AddNumberToObject(e,"start_time",(double)sqlite3_column_int64(st,2));cJSON_AddNumberToObject(e,"duration_minutes",sqlite3_column_int(st,3));cJSON_AddStringToObject(e,"status",(const char*)sqlite3_column_text(st,4));cJSON_AddItemToArray(arr,e);}sqlite3_finalize(st);cJSON_AddItemToObject(root,"exams",arr);char *p=cJSON_PrintUnformatted(root);if(p&&strlen(p)<RESPONSE_SIZE-512){char b[RESPONSE_SIZE];snprintf(b,sizeof(b),"%s",p);respond(fd,200,"OK",b,NULL);}else respond(fd,500,"Internal Server Error","{\"error\":\"response_too_large\"}\n",NULL);if(p)cJSON_free(p);cJSON_Delete(root);return;
    }

    /* Admin: publish a draft exam: POST /api/v1/admin/exams/{id}/publish */
    {
        int eid=0; char tail[32]={0};
        if (strcmp(method,"POST")==0 && sscanf(path,"/api/v1/admin/exams/%d/%31s",&eid,tail)==2 && strcmp(tail,"publish")==0 && eid>0) {
            char token[65]; AuthUser user;
            if(get_session_cookie(request,token,sizeof(token))!=0||auth_validate_session(&auth,token,&user)!=0){respond(fd,401,"Unauthorized","{\"error\":\"authentication_required\"}\n",NULL);return;}
            if(strcmp(user.role,"admin")!=0){respond(fd,403,"Forbidden","{\"error\":\"admin_role_required\"}\n",NULL);return;}
            sqlite3_stmt *st=NULL; if(sqlite3_prepare_v2(auth.db,"UPDATE exams SET status=CASE WHEN start_time<=? THEN 'active' ELSE 'scheduled' END WHERE id=? AND status='draft'",-1,&st,NULL)!=SQLITE_OK){respond(fd,500,"Internal Server Error","{\"error\":\"database_error\"}\n",NULL);return;}
            sqlite3_bind_int64(st,1,(sqlite3_int64)time(NULL));sqlite3_bind_int(st,2,eid);int rc=sqlite3_step(st);int changed=sqlite3_changes(auth.db);sqlite3_finalize(st);
            if(rc!=SQLITE_DONE){respond(fd,500,"Internal Server Error","{\"error\":\"database_error\"}\n",NULL);}else if(!changed){respond(fd,404,"Not Found","{\"error\":\"draft_exam_not_found\"}\n",NULL);}else respond(fd,200,"OK","{\"status\":\"published\"}\n",NULL);return;
        }
    }

    /* Admin: add a question and its grading key. Demo only: grading key is in this DB. */
    if(strcmp(path,"/api/v1/admin/questions")==0 && strcmp(method,"POST")==0){
        char token[65];AuthUser user;if(get_session_cookie(request,token,sizeof(token))!=0||auth_validate_session(&auth,token,&user)!=0){respond(fd,401,"Unauthorized","{\"error\":\"authentication_required\"}\n",NULL);return;}if(strcmp(user.role,"admin")!=0){respond(fd,403,"Forbidden","{\"error\":\"admin_role_required\"}\n",NULL);return;}
        if(body_length==0){respond(fd,400,"Bad Request","{\"error\":\"json_body_required\"}\n",NULL);return;}char *body=request+header_length;body[body_length]='\0';cJSON *j=cJSON_Parse(body);cJSON *ej=j?cJSON_GetObjectItemCaseSensitive(j,"exam_id"):NULL,*qj=j?cJSON_GetObjectItemCaseSensitive(j,"question_text"):NULL,*tj=j?cJSON_GetObjectItemCaseSensitive(j,"question_type"):NULL,*mj=j?cJSON_GetObjectItemCaseSensitive(j,"marks"):NULL,*aj=j?cJSON_GetObjectItemCaseSensitive(j,"correct_answer"):NULL,*oj=j?cJSON_GetObjectItemCaseSensitive(j,"options"):NULL;
        if(!cJSON_IsNumber(ej)||ej->valuedouble<1||ej->valuedouble!=(int)ej->valuedouble||!cJSON_IsString(qj)||!qj->valuestring||!*qj->valuestring||strlen(qj->valuestring)>2000||!cJSON_IsString(tj)||!cJSON_IsString(aj)||!aj->valuestring||!cJSON_IsNumber(mj)||mj->valuedouble<1||mj->valuedouble>100||mj->valuedouble!=(int)mj->valuedouble||(strcmp(tj->valuestring,"mcq")&&strcmp(tj->valuestring,"integer")&&strcmp(tj->valuestring,"text"))){if(j)cJSON_Delete(j);respond(fd,400,"Bad Request","{\"error\":\"invalid_fields\"}\n",NULL);return;}
        char *opts=cJSON_IsArray(oj)?cJSON_PrintUnformatted(oj):NULL;sqlite3_stmt *st=NULL;const char *sql="INSERT INTO exam_questions(exam_id,question_text,question_type,options_json,marks,position) SELECT ?,?,?,?,?,COALESCE((SELECT MAX(position)+1 FROM exam_questions WHERE exam_id=?),1) WHERE EXISTS(SELECT 1 FROM exams WHERE id=? AND status='draft')";
        if(sqlite3_prepare_v2(auth.db,sql,-1,&st,NULL)!=SQLITE_OK){if(opts)cJSON_free(opts);cJSON_Delete(j);respond(fd,500,"Internal Server Error","{\"error\":\"database_error\"}\n",NULL);return;}int eid=ej->valueint;sqlite3_bind_int(st,1,eid);sqlite3_bind_text(st,2,qj->valuestring,-1,SQLITE_TRANSIENT);sqlite3_bind_text(st,3,tj->valuestring,-1,SQLITE_TRANSIENT);if(opts)sqlite3_bind_text(st,4,opts,-1,SQLITE_TRANSIENT);else sqlite3_bind_null(st,4);sqlite3_bind_int(st,5,mj->valueint);sqlite3_bind_int(st,6,eid);sqlite3_bind_int(st,7,eid);int rc=sqlite3_step(st);int qid=(int)sqlite3_last_insert_rowid(auth.db);int changed=sqlite3_changes(auth.db);sqlite3_finalize(st);if(opts)cJSON_free(opts);
        if(rc==SQLITE_DONE&&changed&&sqlite3_prepare_v2(auth.db,"INSERT INTO grading_keys(question_id,answer_text) VALUES(?,?)",-1,&st,NULL)==SQLITE_OK){sqlite3_bind_int(st,1,qid);sqlite3_bind_text(st,2,aj->valuestring,-1,SQLITE_TRANSIENT);rc=sqlite3_step(st);sqlite3_finalize(st);}else rc=SQLITE_ERROR;cJSON_Delete(j);if(rc!=SQLITE_DONE){respond(fd,400,"Bad Request","{\"error\":\"question_creation_failed_check_draft_exam\"}\n",NULL);return;}char out[128];snprintf(out,sizeof(out),"{\"status\":\"created\",\"question_id\":%d}\n",qid);respond(fd,201,"Created",out,NULL);return;
    }

    /* Start or resume a student attempt only while the published exam is in its time window. */
    {
        int eid=0;char action[24]={0};
        if(strcmp(method,"POST")==0&&sscanf(path,"/api/v1/student/exams/%d/%23s",&eid,action)==2&&strcmp(action,"start")==0&&eid>0){
            char token[65];AuthUser user;if(get_session_cookie(request,token,sizeof(token))!=0||auth_validate_session(&auth,token,&user)!=0){respond(fd,401,"Unauthorized","{\"error\":\"authentication_required\"}\n",NULL);return;}if(strcmp(user.role,"student")!=0){respond(fd,403,"Forbidden","{\"error\":\"student_role_required\"}\n",NULL);return;}
            ApiError error;
            cJSON *response = api_start_exam(eid, user.user_id, &error);
            if (!response) { respond_api_error(fd, error); return; }
            respond_json(fd, 200, "OK", response, NULL);
            cJSON_Delete(response);
            return;
        }
    }


    /* Student paper endpoint: questions and saved answers, never grading keys. */
    {
        int eid=0; char action[32]={0};
        if(strcmp(method,"GET")==0 && sscanf(path,"/api/v1/student/exams/%d/%31s",&eid,action)==2 && strcmp(action,"questions")==0 && eid>0){
            char token[65];AuthUser user;
            if(get_session_cookie(request,token,sizeof(token))!=0||auth_validate_session(&auth,token,&user)!=0){respond(fd,401,"Unauthorized","{\"error\":\"authentication_required\"}\n",NULL);return;}
            if(strcmp(user.role,"student")!=0){respond(fd,403,"Forbidden","{\"error\":\"student_role_required\"}\n",NULL);return;}
            ApiError error;
            cJSON *response = api_get_questions(eid, user.user_id, &error);
            if (!response) { respond_api_error(fd, error); return; }
            respond_json(fd, 200, "OK", response, NULL);
            cJSON_Delete(response);
            return;
        }
    }

    /* Save one answer; each save is durable in SQLite. */
    if(strcmp(path,"/api/v1/student/answers")==0&&strcmp(method,"POST")==0){
        char token[65];AuthUser user;if(get_session_cookie(request,token,sizeof(token))!=0||auth_validate_session(&auth,token,&user)!=0){respond(fd,401,"Unauthorized","{\"error\":\"authentication_required\"}\n",NULL);return;}if(strcmp(user.role,"student")!=0){respond(fd,403,"Forbidden","{\"error\":\"student_role_required\"}\n",NULL);return;}if(!body_length){respond(fd,400,"Bad Request","{\"error\":\"json_body_required\"}\n",NULL);return;}
        char *body=request+header_length;body[body_length]='\0';cJSON *j=cJSON_Parse(body);cJSON *qj=j?cJSON_GetObjectItemCaseSensitive(j,"question_id"):NULL,*aj=j?cJSON_GetObjectItemCaseSensitive(j,"answer"):NULL;if(!cJSON_IsNumber(qj)||qj->valuedouble<1||qj->valuedouble!=(int)qj->valuedouble||!cJSON_IsString(aj)||!aj->valuestring||strlen(aj->valuestring)>4000){if(j)cJSON_Delete(j);respond(fd,400,"Bad Request","{\"error\":\"invalid_fields\"}\n",NULL);return;}
        ApiError error;
        cJSON *response = api_save_answer(user.user_id, qj->valueint, aj->valuestring, &error);
        cJSON_Delete(j);
        if (!response) { respond_api_error(fd, error); return; }
        respond_json(fd, 200, "OK", response, NULL);
        cJSON_Delete(response);
        return;
    }

    /* Submit and grade MCQ/integer answers. Text questions require manual grading. */
    {
        int eid=0;char action[24]={0};if(strcmp(method,"POST")==0&&sscanf(path,"/api/v1/student/exams/%d/%23s",&eid,action)==2&&strcmp(action,"submit")==0&&eid>0){
            char token[65];AuthUser user;if(get_session_cookie(request,token,sizeof(token))!=0||auth_validate_session(&auth,token,&user)!=0){respond(fd,401,"Unauthorized","{\"error\":\"authentication_required\"}\n",NULL);return;}if(strcmp(user.role,"student")!=0){respond(fd,403,"Forbidden","{\"error\":\"student_role_required\"}\n",NULL);return;}
            ApiError error;
            cJSON *response = api_submit_exam(eid, user.user_id, &error);
            if (!response) { respond_api_error(fd, error); return; }
            respond_json(fd, 200, "OK", response, NULL);
            cJSON_Delete(response);
            return;
        }
    }

    /* Admin: compact results report. */
    if(strcmp(path,"/api/v1/admin/results")==0&&strcmp(method,"GET")==0){char token[65];AuthUser user;if(get_session_cookie(request,token,sizeof(token))!=0||auth_validate_session(&auth,token,&user)!=0){respond(fd,401,"Unauthorized","{\"error\":\"authentication_required\"}\n",NULL);return;}if(strcmp(user.role,"admin")!=0){respond(fd,403,"Forbidden","{\"error\":\"admin_role_required\"}\n",NULL);return;}sqlite3_stmt *st=NULL;if(sqlite3_prepare_v2(auth.db,"SELECT e.title,u.username,a.status,COALESCE(a.score,0),a.started_at,a.submitted_at FROM exam_attempts a JOIN exams e ON e.id=a.exam_id JOIN users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT 100",-1,&st,NULL)!=SQLITE_OK){respond(fd,500,"Internal Server Error","{\"error\":\"database_error\"}\n",NULL);return;}cJSON *root=cJSON_CreateObject(),*arr=cJSON_CreateArray();while(sqlite3_step(st)==SQLITE_ROW){cJSON *r=cJSON_CreateObject();cJSON_AddStringToObject(r,"exam",(const char*)sqlite3_column_text(st,0));cJSON_AddStringToObject(r,"student",(const char*)sqlite3_column_text(st,1));cJSON_AddStringToObject(r,"status",(const char*)sqlite3_column_text(st,2));cJSON_AddNumberToObject(r,"score",sqlite3_column_int(st,3));cJSON_AddNumberToObject(r,"started_at",(double)sqlite3_column_int64(st,4));if(sqlite3_column_type(st,5)!=SQLITE_NULL)cJSON_AddNumberToObject(r,"submitted_at",(double)sqlite3_column_int64(st,5));cJSON_AddItemToArray(arr,r);}sqlite3_finalize(st);cJSON_AddItemToObject(root,"results",arr);char *p=cJSON_PrintUnformatted(root);if(p&&strlen(p)<RESPONSE_SIZE-512){char b[RESPONSE_SIZE];snprintf(b,sizeof(b),"%s",p);respond(fd,200,"OK",b,NULL);}else respond(fd,500,"Internal Server Error","{\"error\":\"response_too_large\"}\n",NULL);if(p)cJSON_free(p);cJSON_Delete(root);return;}

    if (strcmp(method, "GET") != 0 && strcmp(method, "POST") != 0 &&
        strcmp(method, "PUT") != 0 && strcmp(method, "DELETE") != 0) {
        respond(fd, 405, "Method Not Allowed", "{\"error\":\"method_not_allowed\"}\n",
                "Allow: GET, POST, PUT, DELETE\r\n");
        return;
    }
    respond(fd, 404, "Not Found", "{\"error\":\"route_not_found\"}\n", NULL);
}

static void configure_client_socket(int fd)
{
    struct timeval timeout = { .tv_sec = CLIENT_TIMEOUT_SECONDS, .tv_usec = 0 };
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
}

static void worker_finished(void)
{
    pthread_mutex_lock(&workers_mutex);
    if (active_workers > 0) active_workers--;
    pthread_cond_signal(&workers_cond);
    pthread_mutex_unlock(&workers_mutex);
}

static void *worker_main(void *argument)
{
    int client_fd = (int)(intptr_t)argument;

    sigset_t blocked;
    sigemptyset(&blocked);
    sigaddset(&blocked, SIGINT);
    sigaddset(&blocked, SIGTERM);
    pthread_sigmask(SIG_BLOCK, &blocked, NULL);

    if (auth_init(&auth, database_path) != 0) {
        respond(client_fd, 503, "Service Unavailable",
                "{\"error\":\"database_unavailable\"}\n", NULL);
    } else {
        handle_request(client_fd);
        auth_close(&auth);
    }
    close(client_fd);
    worker_finished();
    return NULL;
}

static int spawn_worker(int client_fd)
{
    pthread_t thread;
    sigset_t blocked, previous;
    sigemptyset(&blocked);
    sigaddset(&blocked, SIGINT);
    sigaddset(&blocked, SIGTERM);

    pthread_mutex_lock(&workers_mutex);
    active_workers++;
    pthread_mutex_unlock(&workers_mutex);

    /* Block shutdown signals while the worker is created so it inherits the mask. */
    pthread_sigmask(SIG_BLOCK, &blocked, &previous);
    int rc = pthread_create(&thread, NULL, worker_main, (void *)(intptr_t)client_fd);
    pthread_sigmask(SIG_SETMASK, &previous, NULL);

    if (rc != 0) {
        worker_finished();
        return -1;
    }
    pthread_detach(thread);
    return 0;
}

static void wait_for_workers(void)
{
    pthread_mutex_lock(&workers_mutex);
    while (active_workers > 0)
        pthread_cond_wait(&workers_cond, &workers_mutex);
    pthread_mutex_unlock(&workers_mutex);
}

static void shutdown_signal_handler(int signal_number)
{
    (void)signal_number;
    shutdown_requested = 1;
}

static int install_signal_handlers(void)
{
    struct sigaction action;
    memset(&action, 0, sizeof(action));
    action.sa_handler = shutdown_signal_handler;
    sigemptyset(&action.sa_mask);
    action.sa_flags = 0;
    if (sigaction(SIGINT, &action, NULL) != 0) return -1;
    if (sigaction(SIGTERM, &action, NULL) != 0) return -1;

    memset(&action, 0, sizeof(action));
    action.sa_handler = SIG_IGN;
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGPIPE, &action, NULL) != 0) return -1;
    return 0;
}

static int parse_port(const char *text, int *port)
{
    if (!text || !port) return -1;
    errno = 0;
    char *end = NULL;
    long value = strtol(text, &end, 10);
    if (errno != 0 || !end || *end != '\0' || value < 1 || value > 65535) return -1;
    *port = (int)value;
    return 0;
}

static void print_usage(const char *program, FILE *stream)
{
    fprintf(stream,
            "Usage: %s [--port PORT] [--db PATH] [--addr ADDRESS]\n"
            "  --port PORT       TCP port to listen on (default: %d)\n"
            "  --db PATH         SQLite database path (default: %s)\n"
            "  --addr ADDRESS    IPv4 address to bind (default: %s)\n"
            "  -h, --help        Show this help\n",
            program, DEFAULT_PORT, DEFAULT_DB, DEFAULT_ADDR);
}

static int parse_options(int argc, char **argv, ServerConfig *config)
{
    config->port = DEFAULT_PORT;
    config->db_path = DEFAULT_DB;
    config->addr = DEFAULT_ADDR;

    static const struct option options[] = {
        { "port", required_argument, NULL, 'p' },
        { "db",   required_argument, NULL, 'd' },
        { "addr", required_argument, NULL, 'a' },
        { "help", no_argument,       NULL, 'h' },
        { NULL,   0,                 NULL, 0 }
    };

    int option;
    while ((option = getopt_long(argc, argv, "p:d:a:h", options, NULL)) != -1) {
        switch (option) {
        case 'p':
            if (parse_port(optarg, &config->port) != 0) {
                fprintf(stderr, "Invalid port: %s\n", optarg);
                return -1;
            }
            break;
        case 'd':
            if (!optarg || optarg[0] == '\0') {
                fprintf(stderr, "Database path cannot be empty.\n");
                return -1;
            }
            config->db_path = optarg;
            break;
        case 'a':
            if (!optarg || optarg[0] == '\0') {
                fprintf(stderr, "Bind address cannot be empty.\n");
                return -1;
            }
            config->addr = optarg;
            break;
        case 'h':
            print_usage(argv[0], stdout);
            exit(EXIT_SUCCESS);
        default:
            print_usage(argv[0], stderr);
            return -1;
        }
    }

    if (optind != argc) {
        fprintf(stderr, "Unexpected argument: %s\n", argv[optind]);
        print_usage(argv[0], stderr);
        return -1;
    }
    return 0;
}

int main(int argc, char **argv)
{
    ServerConfig config;
    if (parse_options(argc, argv, &config) != 0) return EXIT_FAILURE;
    database_path = config.db_path;

    if (install_signal_handlers() != 0) {
        perror("sigaction");
        return EXIT_FAILURE;
    }

    /* Validate/open the database once before accepting traffic. */
    Auth bootstrap_auth = {0};
    if (auth_init(&bootstrap_auth, database_path) != 0) {
        fprintf(stderr, "Could not initialize authentication database at %s.\n",
                database_path);
        return EXIT_FAILURE;
    }
    auth_close(&bootstrap_auth);

    struct sockaddr_in address = {0};
    address.sin_family = AF_INET;
    address.sin_port = htons((uint16_t)config.port);
    if (inet_pton(AF_INET, config.addr, &address.sin_addr) != 1) {
        fprintf(stderr, "Invalid IPv4 bind address: %s\n", config.addr);
        return EXIT_FAILURE;
    }

    int server_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (server_fd == -1) {
        perror("socket");
        return EXIT_FAILURE;
    }
    int opt = 1;
    setsockopt(server_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
    if (bind(server_fd, (struct sockaddr *)&address, sizeof(address)) == -1) {
        perror("bind");
        close(server_fd);
        return EXIT_FAILURE;
    }
    if (listen(server_fd, 128) == -1) {
        perror("listen");
        close(server_fd);
        return EXIT_FAILURE;
    }
    printf("Exam backend listening on %s:%d (db: %s)\n",
           config.addr, config.port, database_path);
    fflush(stdout);

    while (!shutdown_requested) {
        int client_fd = accept(server_fd, NULL, NULL);
        if (client_fd == -1) {
            if (errno == EINTR) continue;
            if (!shutdown_requested) perror("accept");
            continue;
        }
        if (shutdown_requested) {
            close(client_fd);
            break;
        }
        configure_client_socket(client_fd);
        if (spawn_worker(client_fd) != 0) {
            perror("pthread_create");
            close(client_fd);
        }
    }

    close(server_fd);
    printf("Shutting down; waiting for active connections...\n");
    fflush(stdout);
    wait_for_workers();
    pthread_cond_destroy(&workers_cond);
    pthread_mutex_destroy(&workers_mutex);
    pthread_cond_destroy(&login_sem.cond);
    pthread_mutex_destroy(&login_sem.mutex);
    printf("Stopped.\n");
    return EXIT_SUCCESS;
}
