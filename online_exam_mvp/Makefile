CC = gcc
CFLAGS = -Wall -Wextra -Wpedantic -O2 -g -Iinclude $(shell pkg-config --cflags sqlite3 libsodium libcjson)
LDLIBS = $(shell pkg-config --libs sqlite3 libsodium libcjson) -pthread

.PHONY: all clean schema
all: exam_server create_user

exam_server: src/server.c src/auth.c include/auth.h
	$(CC) $(CFLAGS) src/server.c src/auth.c -o $@ $(LDLIBS)

create_user: src/create_user.c src/auth.c include/auth.h
	$(CC) $(CFLAGS) src/create_user.c src/auth.c -o $@ $(LDLIBS)

schema:
	sqlite3 database/exam.db < database/schema.sql

clean:
	rm -f exam_server create_user src/*.o
