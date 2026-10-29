#define _GNU_SOURCE

#include "auth.h"
#include <cjson/cJSON.h>
#include <arpa/inet.h>
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define PORT 8080
#define REQUEST_SIZE 8192
#define RESPONSE_SIZE 4096
#define SESSION_COOKIE "exam_session"

static Auth auth;

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
        AuthUser user;
        int result = auth_login(&auth, username_json->valuestring,
                                password_json->valuestring, token, sizeof(token), &user);
        cJSON_Delete(json);
        if (result != 0) {
            respond(fd, 401, "Unauthorized", "{\"error\":\"invalid_credentials\"}\n", NULL);
            return;
        }
        cJSON *response_json = cJSON_CreateObject();
        cJSON *user_json = cJSON_CreateObject();
        cJSON_AddStringToObject(response_json, "status", "ok");
        cJSON_AddNumberToObject(user_json, "id", user.user_id);
        cJSON_AddStringToObject(user_json, "username", user.username);
        cJSON_AddStringToObject(user_json, "role", user.role);
        cJSON_AddItemToObject(response_json, "user", user_json);
        char *printed = cJSON_PrintUnformatted(response_json);
        char response_body[512];
        snprintf(response_body, sizeof(response_body), "%s\n", printed ? printed : "{}");
        char headers[256];
        snprintf(headers, sizeof(headers),
                 "Set-Cookie: %s=%s; Path=/backend/; HttpOnly; SameSite=Lax\r\n",
                 SESSION_COOKIE, token);
        respond(fd, 200, "OK", response_body, headers);
        if (printed) cJSON_free(printed);
        cJSON_Delete(response_json);
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
            sqlite3_stmt *st=NULL;const char *sql="INSERT OR IGNORE INTO exam_attempts(exam_id,user_id,started_at) SELECT id,?,? FROM exams WHERE id=? AND status IN ('scheduled','active') AND unixepoch()>=start_time AND unixepoch()<start_time+duration_minutes*60";
            if(sqlite3_prepare_v2(auth.db,sql,-1,&st,NULL)!=SQLITE_OK){respond(fd,500,"Internal Server Error","{\"error\":\"database_error\"}\n",NULL);return;}sqlite3_bind_int(st,1,user.user_id);sqlite3_bind_int64(st,2,(sqlite3_int64)time(NULL));sqlite3_bind_int(st,3,eid);int rc=sqlite3_step(st);sqlite3_finalize(st);if(rc!=SQLITE_DONE){respond(fd,500,"Internal Server Error","{\"error\":\"database_error\"}\n",NULL);return;}
            if(sqlite3_prepare_v2(auth.db,"SELECT id,status FROM exam_attempts WHERE exam_id=? AND user_id=?",-1,&st,NULL)!=SQLITE_OK){respond(fd,500,"Internal Server Error","{\"error\":\"database_error\"}\n",NULL);return;}sqlite3_bind_int(st,1,eid);sqlite3_bind_int(st,2,user.user_id);rc=sqlite3_step(st);if(rc==SQLITE_ROW){char out[128];snprintf(out,sizeof(out),"{\"attempt_id\":%d,\"status\":\"%s\"}\n",sqlite3_column_int(st,0),(const char*)sqlite3_column_text(st,1));sqlite3_finalize(st);respond(fd,200,"OK",out,NULL);}else{sqlite3_finalize(st);respond(fd,403,"Forbidden","{\"error\":\"exam_not_available_now\"}\n",NULL);}return;
        }
    }


    /* Student paper endpoint: questions and saved answers, never grading keys. */
    {
        int eid=0; char action[32]={0};
        if(strcmp(method,"GET")==0 && sscanf(path,"/api/v1/student/exams/%d/%31s",&eid,action)==2 && strcmp(action,"questions")==0 && eid>0){
            char token[65];AuthUser user;
            if(get_session_cookie(request,token,sizeof(token))!=0||auth_validate_session(&auth,token,&user)!=0){respond(fd,401,"Unauthorized","{\"error\":\"authentication_required\"}\n",NULL);return;}
            if(strcmp(user.role,"student")!=0){respond(fd,403,"Forbidden","{\"error\":\"student_role_required\"}\n",NULL);return;}
            sqlite3_stmt *st=NULL;const char *check="SELECT a.id FROM exam_attempts a JOIN exams e ON e.id=a.exam_id WHERE a.exam_id=? AND a.user_id=? AND a.status='in_progress' AND unixepoch()>=e.start_time AND unixepoch()<e.start_time+e.duration_minutes*60";
            if(sqlite3_prepare_v2(auth.db,check,-1,&st,NULL)!=SQLITE_OK){respond(fd,500,"Internal Server Error","{\"error\":\"database_error\"}\n",NULL);return;}
            sqlite3_bind_int(st,1,eid);sqlite3_bind_int(st,2,user.user_id);int rc=sqlite3_step(st);if(rc!=SQLITE_ROW){sqlite3_finalize(st);respond(fd,403,"Forbidden","{\"error\":\"attempt_not_active\"}\n",NULL);return;}int aid=sqlite3_column_int(st,0);sqlite3_finalize(st);
            const char *sql="SELECT q.id,q.question_text,q.question_type,q.options_json,q.marks,COALESCE(a.answer_text,'') FROM exam_questions q LEFT JOIN student_answers a ON a.question_id=q.id AND a.attempt_id=? WHERE q.exam_id=? ORDER BY q.position,q.id";
            if(sqlite3_prepare_v2(auth.db,sql,-1,&st,NULL)!=SQLITE_OK){respond(fd,500,"Internal Server Error","{\"error\":\"database_error\"}\n",NULL);return;}sqlite3_bind_int(st,1,aid);sqlite3_bind_int(st,2,eid);cJSON *root=cJSON_CreateObject(),*arr=cJSON_CreateArray();while(sqlite3_step(st)==SQLITE_ROW){cJSON *q=cJSON_CreateObject();cJSON_AddNumberToObject(q,"id",sqlite3_column_int(st,0));cJSON_AddStringToObject(q,"question_text",(const char*)sqlite3_column_text(st,1));cJSON_AddStringToObject(q,"question_type",(const char*)sqlite3_column_text(st,2));const char *options=(const char*)sqlite3_column_text(st,3);if(options){cJSON *o=cJSON_Parse(options);if(o)cJSON_AddItemToObject(q,"options",o);}cJSON_AddNumberToObject(q,"marks",sqlite3_column_int(st,4));cJSON_AddStringToObject(q,"answer",(const char*)sqlite3_column_text(st,5));cJSON_AddItemToArray(arr,q);}sqlite3_finalize(st);cJSON_AddNumberToObject(root,"exam_id",eid);cJSON_AddItemToObject(root,"questions",arr);char *p=cJSON_PrintUnformatted(root);if(p&&strlen(p)<RESPONSE_SIZE-512){char out[RESPONSE_SIZE];snprintf(out,sizeof(out),"%s",p);respond(fd,200,"OK",out,NULL);}else respond(fd,500,"Internal Server Error","{\"error\":\"paper_too_large_for_demo_server\"}\n",NULL);if(p)cJSON_free(p);cJSON_Delete(root);return;
        }
    }

    /* Save one answer; each save is durable in SQLite. */
    if(strcmp(path,"/api/v1/student/answers")==0&&strcmp(method,"POST")==0){
        char token[65];AuthUser user;if(get_session_cookie(request,token,sizeof(token))!=0||auth_validate_session(&auth,token,&user)!=0){respond(fd,401,"Unauthorized","{\"error\":\"authentication_required\"}\n",NULL);return;}if(strcmp(user.role,"student")!=0){respond(fd,403,"Forbidden","{\"error\":\"student_role_required\"}\n",NULL);return;}if(!body_length){respond(fd,400,"Bad Request","{\"error\":\"json_body_required\"}\n",NULL);return;}
        char *body=request+header_length;body[body_length]='\0';cJSON *j=cJSON_Parse(body);cJSON *qj=j?cJSON_GetObjectItemCaseSensitive(j,"question_id"):NULL,*aj=j?cJSON_GetObjectItemCaseSensitive(j,"answer"):NULL;if(!cJSON_IsNumber(qj)||qj->valuedouble<1||qj->valuedouble!=(int)qj->valuedouble||!cJSON_IsString(aj)||!aj->valuestring||strlen(aj->valuestring)>4000){if(j)cJSON_Delete(j);respond(fd,400,"Bad Request","{\"error\":\"invalid_fields\"}\n",NULL);return;}
        sqlite3_stmt *st=NULL;const char *sql="INSERT INTO student_answers(attempt_id,question_id,answer_text,updated_at) SELECT a.id,q.id,?,? FROM exam_attempts a JOIN exams e ON e.id=a.exam_id JOIN exam_questions q ON q.exam_id=e.id WHERE a.user_id=? AND a.status='in_progress' AND q.id=? AND unixepoch()<e.start_time+e.duration_minutes*60 ON CONFLICT(attempt_id,question_id) DO UPDATE SET answer_text=excluded.answer_text,updated_at=excluded.updated_at";
        if(sqlite3_prepare_v2(auth.db,sql,-1,&st,NULL)!=SQLITE_OK){cJSON_Delete(j);respond(fd,500,"Internal Server Error","{\"error\":\"database_error\"}\n",NULL);return;}sqlite3_bind_text(st,1,aj->valuestring,-1,SQLITE_TRANSIENT);sqlite3_bind_int64(st,2,(sqlite3_int64)time(NULL));sqlite3_bind_int(st,3,user.user_id);sqlite3_bind_int(st,4,qj->valueint);int rc=sqlite3_step(st);int changed=sqlite3_changes(auth.db);sqlite3_finalize(st);cJSON_Delete(j);if(rc==SQLITE_DONE&&changed)respond(fd,200,"OK","{\"status\":\"saved\"}\n",NULL);else respond(fd,403,"Forbidden","{\"error\":\"answer_not_saved_exam_inactive_or_question_invalid\"}\n",NULL);return;
    }

    /* Submit and grade MCQ/integer answers. Text questions require manual grading. */
    {
        int eid=0;char action[24]={0};if(strcmp(method,"POST")==0&&sscanf(path,"/api/v1/student/exams/%d/%23s",&eid,action)==2&&strcmp(action,"submit")==0&&eid>0){
            char token[65];AuthUser user;if(get_session_cookie(request,token,sizeof(token))!=0||auth_validate_session(&auth,token,&user)!=0){respond(fd,401,"Unauthorized","{\"error\":\"authentication_required\"}\n",NULL);return;}if(strcmp(user.role,"student")!=0){respond(fd,403,"Forbidden","{\"error\":\"student_role_required\"}\n",NULL);return;}
            sqlite3_stmt *st=NULL;const char *sql="SELECT a.id FROM exam_attempts a JOIN exams e ON e.id=a.exam_id WHERE a.exam_id=? AND a.user_id=? AND a.status='in_progress' AND unixepoch()>=e.start_time AND unixepoch()<e.start_time+e.duration_minutes*60";if(sqlite3_prepare_v2(auth.db,sql,-1,&st,NULL)!=SQLITE_OK){respond(fd,500,"Internal Server Error","{\"error\":\"database_error\"}\n",NULL);return;}sqlite3_bind_int(st,1,eid);sqlite3_bind_int(st,2,user.user_id);int rc=sqlite3_step(st);if(rc!=SQLITE_ROW){sqlite3_finalize(st);respond(fd,403,"Forbidden","{\"error\":\"attempt_not_active\"}\n",NULL);return;}int aid=sqlite3_column_int(st,0);sqlite3_finalize(st);
            const char *grade="SELECT COALESCE(SUM(CASE WHEN lower(trim(ans.answer_text))=lower(trim(k.answer_text)) THEN q.marks ELSE 0 END),0) FROM exam_questions q JOIN grading_keys k ON k.question_id=q.id LEFT JOIN student_answers ans ON ans.question_id=q.id AND ans.attempt_id=? WHERE q.exam_id=? AND q.question_type IN ('mcq','integer')";if(sqlite3_prepare_v2(auth.db,grade,-1,&st,NULL)!=SQLITE_OK){respond(fd,500,"Internal Server Error","{\"error\":\"grading_error\"}\n",NULL);return;}sqlite3_bind_int(st,1,aid);sqlite3_bind_int(st,2,eid);rc=sqlite3_step(st);int score=rc==SQLITE_ROW?sqlite3_column_int(st,0):0;sqlite3_finalize(st);
            if(sqlite3_prepare_v2(auth.db,"UPDATE exam_attempts SET submitted_at=?,status='submitted',score=? WHERE id=? AND status='in_progress'",-1,&st,NULL)!=SQLITE_OK){respond(fd,500,"Internal Server Error","{\"error\":\"database_error\"}\n",NULL);return;}sqlite3_bind_int64(st,1,(sqlite3_int64)time(NULL));sqlite3_bind_int(st,2,score);sqlite3_bind_int(st,3,aid);rc=sqlite3_step(st);sqlite3_finalize(st);if(rc!=SQLITE_DONE){respond(fd,500,"Internal Server Error","{\"error\":\"submit_failed\"}\n",NULL);return;}char out[128];snprintf(out,sizeof(out),"{\"status\":\"submitted\",\"score\":%d}\n",score);respond(fd,200,"OK",out,NULL);return;
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

int main(void)
{
    if (auth_init(&auth, "database/exam.db") != 0) {
        fprintf(stderr, "Could not initialize authentication database.\n");
        return EXIT_FAILURE;
    }
    int server_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (server_fd == -1) {
        perror("socket"); auth_close(&auth); return EXIT_FAILURE;
    }
    int opt = 1;
    setsockopt(server_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
    struct sockaddr_in address = {0};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    address.sin_port = htons(PORT);
    if (bind(server_fd, (struct sockaddr *)&address, sizeof(address)) == -1) {
        perror("bind"); close(server_fd); auth_close(&auth); return EXIT_FAILURE;
    }
    if (listen(server_fd, 32) == -1) {
        perror("listen"); close(server_fd); auth_close(&auth); return EXIT_FAILURE;
    }
    printf("Exam backend listening on 127.0.0.1:%d\n", PORT);
    while (1) {
        int client_fd = accept(server_fd, NULL, NULL);
        if (client_fd == -1) {
            if (errno == EINTR) continue;
            perror("accept"); continue;
        }
        handle_request(client_fd);
        close(client_fd);
    }
    close(server_fd);
    auth_close(&auth);
    return EXIT_SUCCESS;
}
