#define _GNU_SOURCE
#include "auth.h"
#include <getopt.h>
#include <sodium.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define PASSWORD_MAX 1024

static void usage(const char *program, FILE *stream)
{
    fprintf(stream,
            "Usage: %s [--db PATH] [--password-stdin] USERNAME admin|student\n"
            "  --db PATH          SQLite database path (default: database/exam.db)\n"
            "  --password-stdin   Read one password line from stdin (for scripts)\n"
            "  -h, --help         Show this help\n",
            program);
}

/*
 * Read exactly one password line from stdin. The password is never placed on
 * the command line, so it does not appear in shell history or the process list.
 * A line longer than the buffer is rejected instead of being silently cut.
 */
static int read_stdin_password(char *buffer, size_t capacity)
{
    if (!buffer || capacity < 2) return -1;
    if (!fgets(buffer, (int)capacity, stdin)) return -1;

    size_t length = strlen(buffer);
    if (length > 0 && buffer[length - 1] != '\n') {
        int ch;
        int extra = 0;
        while ((ch = getchar()) != '\n' && ch != EOF) extra = 1;
        if (extra) return -2;
    }
    while (length > 0 && (buffer[length - 1] == '\n' || buffer[length - 1] == '\r'))
        buffer[--length] = '\0';
    return 0;
}

static int prompt_passwords(char *password, size_t capacity)
{
    char *first = getpass("Password (minimum 12 characters): ");
    if (!first) return -1;
    snprintf(password, capacity, "%s", first);

    char *second = getpass("Repeat password: ");
    if (!second || strcmp(password, second) != 0) {
        fprintf(stderr, "Passwords do not match.\n");
        return -1;
    }
    return 0;
}

int main(int argc, char **argv)
{
    const char *db_path = "database/exam.db";
    int password_from_stdin = 0;

    static const struct option options[] = {
        { "db",             required_argument, NULL, 'd' },
        { "password-stdin", no_argument,       NULL, 's' },
        { "help",           no_argument,       NULL, 'h' },
        { NULL,             0,                 NULL, 0 }
    };

    int option;
    while ((option = getopt_long(argc, argv, "d:sh", options, NULL)) != -1) {
        switch (option) {
        case 'd':
            if (!optarg || optarg[0] == '\0') {
                fprintf(stderr, "Database path cannot be empty.\n");
                return 2;
            }
            db_path = optarg;
            break;
        case 's':
            password_from_stdin = 1;
            break;
        case 'h':
            usage(argv[0], stdout);
            return 0;
        default:
            usage(argv[0], stderr);
            return 2;
        }
    }

    if (optind + 2 != argc) {
        usage(argv[0], stderr);
        return 2;
    }
    const char *username = argv[optind];
    const char *role = argv[optind + 1];
    if (strcmp(role, "admin") != 0 && strcmp(role, "student") != 0) {
        fprintf(stderr, "Role must be 'admin' or 'student'.\n");
        return 2;
    }

    char password[PASSWORD_MAX + 1];
    memset(password, 0, sizeof(password));
    int password_result;
    if (password_from_stdin) {
        password_result = read_stdin_password(password, sizeof(password));
        if (password_result == -2) {
            fprintf(stderr, "Password is too long (maximum %d characters).\n", PASSWORD_MAX);
            return 1;
        }
    } else {
        password_result = prompt_passwords(password, sizeof(password));
    }
    if (password_result != 0) {
        sodium_memzero(password, sizeof(password));
        return 1;
    }

    Auth auth = {0};
    if (auth_init(&auth, db_path) != 0) {
        fprintf(stderr, "Cannot open database: %s\n", db_path);
        sodium_memzero(password, sizeof(password));
        return 1;
    }

    int result = auth_create_user(&auth, username, password, role);
    auth_close(&auth);
    sodium_memzero(password, sizeof(password));

    if (result != 0) {
        fprintf(stderr,
                "Could not create user (username may already exist, "
                "password must be at least 12 characters, or the username is invalid).\n");
        return 1;
    }
    printf("Created %s account '%s'.\n", role, username);
    return 0;
}
