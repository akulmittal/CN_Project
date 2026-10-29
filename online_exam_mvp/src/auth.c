#define _GNU_SOURCE
#include "auth.h"
#include <sodium.h>
#include <sqlite3.h>
#include <string.h>
#include <stdio.h>
#include <time.h>
#include <stdint.h>

static int exec_sql(sqlite3 *db, const char *sql) {
    char *err = NULL;
    int rc = sqlite3_exec(db, sql, NULL, NULL, &err);
    sqlite3_free(err);
    return rc == SQLITE_OK ? 0 : -1;
}
int auth_init(Auth *auth, const char *path) {
    if (!auth || !path || sodium_init() < 0) return -1;
    auth->db = NULL;
    if (sqlite3_open_v2(path, &auth->db, SQLITE_OPEN_READWRITE|SQLITE_OPEN_CREATE|SQLITE_OPEN_FULLMUTEX, NULL) != SQLITE_OK) return -1;
    sqlite3_busy_timeout(auth->db, 5000);
    if (exec_sql(auth->db, "PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL;"
      "CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT NOT NULL UNIQUE,password_hash TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('student','admin')),is_active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);"
      "CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY AUTOINCREMENT,token_hash TEXT NOT NULL UNIQUE,user_id INTEGER NOT NULL,expires_at INTEGER NOT NULL,created_at INTEGER NOT NULL,FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);"
      "CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);"
      "CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);"
      "CREATE TABLE IF NOT EXISTS exams(id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,start_time INTEGER NOT NULL,duration_minutes INTEGER NOT NULL CHECK(duration_minutes BETWEEN 1 AND 1440),status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','scheduled','active','completed')),created_by INTEGER NOT NULL,created_at INTEGER NOT NULL DEFAULT(unixepoch()),FOREIGN KEY(created_by) REFERENCES users(id));"
      "CREATE INDEX IF NOT EXISTS idx_exams_start_time ON exams(start_time);"
      "CREATE TABLE IF NOT EXISTS exam_questions(id INTEGER PRIMARY KEY AUTOINCREMENT,exam_id INTEGER NOT NULL,question_text TEXT NOT NULL,question_type TEXT NOT NULL CHECK(question_type IN ('mcq','integer','text')),options_json TEXT,marks INTEGER NOT NULL DEFAULT 1 CHECK(marks>0),position INTEGER NOT NULL DEFAULT 1,FOREIGN KEY(exam_id) REFERENCES exams(id) ON DELETE CASCADE);"
      "CREATE INDEX IF NOT EXISTS idx_exam_questions_exam_id ON exam_questions(exam_id);"
      "CREATE TABLE IF NOT EXISTS exam_attempts(id INTEGER PRIMARY KEY AUTOINCREMENT,exam_id INTEGER NOT NULL,user_id INTEGER NOT NULL,started_at INTEGER NOT NULL,submitted_at INTEGER,status TEXT NOT NULL DEFAULT 'in_progress' CHECK(status IN ('in_progress','submitted','graded')),score INTEGER,UNIQUE(exam_id,user_id),FOREIGN KEY(exam_id) REFERENCES exams(id),FOREIGN KEY(user_id) REFERENCES users(id));"
      "CREATE TABLE IF NOT EXISTS student_answers(id INTEGER PRIMARY KEY AUTOINCREMENT,attempt_id INTEGER NOT NULL,question_id INTEGER NOT NULL,answer_text TEXT NOT NULL DEFAULT '',updated_at INTEGER NOT NULL DEFAULT(unixepoch()),UNIQUE(attempt_id,question_id),FOREIGN KEY(attempt_id) REFERENCES exam_attempts(id) ON DELETE CASCADE,FOREIGN KEY(question_id) REFERENCES exam_questions(id));"
      "CREATE TABLE IF NOT EXISTS grading_keys(question_id INTEGER PRIMARY KEY,answer_text TEXT NOT NULL,FOREIGN KEY(question_id) REFERENCES exam_questions(id) ON DELETE CASCADE);")) { auth_close(auth); return -1; }
    return 0;
}
void auth_close(Auth *auth) { if (auth && auth->db) { sqlite3_close(auth->db); auth->db=NULL; } }
int auth_create_user(Auth *auth,const char *username,const char *password,const char *role) {
    if(!auth||!auth->db||!username||!password||!role||!*username||strlen(username)>128||strlen(password)<12||strlen(password)>1024||(strcmp(role,"student")&&strcmp(role,"admin"))) return -1;
    char hash[crypto_pwhash_STRBYTES];
    if(crypto_pwhash_str(hash,password,strlen(password),crypto_pwhash_OPSLIMIT_INTERACTIVE,crypto_pwhash_MEMLIMIT_INTERACTIVE)!=0) return -1;
    sqlite3_stmt *s=NULL; const char *sql="INSERT INTO users(username,password_hash,role) VALUES(?,?,?)";
    if(sqlite3_prepare_v2(auth->db,sql,-1,&s,NULL)!=SQLITE_OK)return -1;
    sqlite3_bind_text(s,1,username,-1,SQLITE_TRANSIENT);sqlite3_bind_text(s,2,hash,-1,SQLITE_TRANSIENT);sqlite3_bind_text(s,3,role,-1,SQLITE_TRANSIENT);
    int rc=sqlite3_step(s);sqlite3_finalize(s);return rc==SQLITE_DONE?0:-1;
}
static void token_hash(const char *token,unsigned char out[crypto_generichash_BYTES]) { crypto_generichash(out,crypto_generichash_BYTES,(const unsigned char*)token,strlen(token),NULL,0); }
int auth_login(Auth *auth,const char *username,const char *password,char *token,size_t token_size,AuthUser *user) {
    if(!auth||!auth->db||!username||!password||!token||token_size<65||!user)return -1;
    sqlite3_stmt *s=NULL; const char *sql="SELECT id,username,password_hash,role FROM users WHERE username=? AND is_active=1";
    if(sqlite3_prepare_v2(auth->db,sql,-1,&s,NULL)!=SQLITE_OK)return -1;
    sqlite3_bind_text(s,1,username,-1,SQLITE_TRANSIENT); int rc=sqlite3_step(s);
    if(rc!=SQLITE_ROW){sqlite3_finalize(s);return -1;}
    int uid=sqlite3_column_int(s,0); const char *dbuser=(const char*)sqlite3_column_text(s,1); const char *hash=(const char*)sqlite3_column_text(s,2); const char *role=(const char*)sqlite3_column_text(s,3);
    char hashcopy[crypto_pwhash_STRBYTES]; AuthUser u={0};
    if(!dbuser||!hash||!role||strlen(hash)>=sizeof(hashcopy)){sqlite3_finalize(s);return -1;}
    snprintf(hashcopy,sizeof(hashcopy),"%s",hash);u.user_id=uid;snprintf(u.username,sizeof(u.username),"%s",dbuser);snprintf(u.role,sizeof(u.role),"%s",role);sqlite3_finalize(s);
    if(crypto_pwhash_str_verify(hashcopy,password,strlen(password))!=0)return -1;
    unsigned char random[32]; randombytes_buf(random,sizeof(random)); sodium_bin2hex(token,token_size,random,sizeof(random)); sodium_memzero(random,sizeof(random));
    unsigned char digest[crypto_generichash_BYTES];token_hash(token,digest);
    if(sqlite3_prepare_v2(auth->db,"INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",-1,&s,NULL)!=SQLITE_OK)return -1;
    char hex[crypto_generichash_BYTES*2+1];sodium_bin2hex(hex,sizeof(hex),digest,sizeof(digest));sqlite3_bind_text(s,1,hex,-1,SQLITE_TRANSIENT);sqlite3_bind_int(s,2,uid);sqlite3_bind_int64(s,3,(sqlite3_int64)time(NULL)+8*60*60);sqlite3_bind_int64(s,4,(sqlite3_int64)time(NULL));rc=sqlite3_step(s);sqlite3_finalize(s);sodium_memzero(digest,sizeof(digest));if(rc!=SQLITE_DONE)return -1;*user=u;return 0;
}
int auth_validate_session(Auth *auth,const char *token,AuthUser *user) {
    if(!auth||!auth->db||!token||strlen(token)!=64||!user)return -1;
    unsigned char digest[crypto_generichash_BYTES];token_hash(token,digest);char hex[crypto_generichash_BYTES*2+1];sodium_bin2hex(hex,sizeof(hex),digest,sizeof(digest));sodium_memzero(digest,sizeof(digest));
    sqlite3_stmt *s=NULL;const char *sql="SELECT u.id,u.username,u.role FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.is_active=1";
    if(sqlite3_prepare_v2(auth->db,sql,-1,&s,NULL)!=SQLITE_OK)return -1;sqlite3_bind_text(s,1,hex,-1,SQLITE_TRANSIENT);sqlite3_bind_int64(s,2,(sqlite3_int64)time(NULL));int rc=sqlite3_step(s);if(rc==SQLITE_ROW){user->user_id=sqlite3_column_int(s,0);snprintf(user->username,sizeof(user->username),"%s",(const char*)sqlite3_column_text(s,1));snprintf(user->role,sizeof(user->role),"%s",(const char*)sqlite3_column_text(s,2));}sqlite3_finalize(s);return rc==SQLITE_ROW?0:-1;
}
int auth_logout(Auth *auth,const char *token) {
    if(!auth||!auth->db||!token||strlen(token)!=64)return -1;unsigned char digest[crypto_generichash_BYTES];token_hash(token,digest);char hex[crypto_generichash_BYTES*2+1];sodium_bin2hex(hex,sizeof(hex),digest,sizeof(digest));sodium_memzero(digest,sizeof(digest));sqlite3_stmt *s=NULL;if(sqlite3_prepare_v2(auth->db,"DELETE FROM sessions WHERE token_hash=?",-1,&s,NULL)!=SQLITE_OK)return -1;sqlite3_bind_text(s,1,hex,-1,SQLITE_TRANSIENT);int rc=sqlite3_step(s);int changes=sqlite3_changes(auth->db);sqlite3_finalize(s);return rc==SQLITE_DONE&&changes>0?0:-1;
}
