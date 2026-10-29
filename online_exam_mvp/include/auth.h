#ifndef AUTH_H
#define AUTH_H
#include <sqlite3.h>
#include <stddef.h>
typedef struct { sqlite3 *db; } Auth;
typedef struct { int user_id; char username[129]; char role[16]; } AuthUser;
int auth_init(Auth *auth, const char *database_path);
void auth_close(Auth *auth);
int auth_create_user(Auth *auth, const char *username, const char *password, const char *role);
int auth_login(Auth *auth, const char *username, const char *password, char *token, size_t token_size, AuthUser *user);
int auth_validate_session(Auth *auth, const char *token, AuthUser *user);
int auth_logout(Auth *auth, const char *token);
#endif
