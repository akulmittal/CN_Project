#define _GNU_SOURCE
#include "auth.h"
#include <sodium.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
int main(int argc,char **argv){
    if(argc!=3 || (strcmp(argv[2],"admin")&&strcmp(argv[2],"student"))){fprintf(stderr,"Usage: %s USERNAME admin|student\n",argv[0]);return 2;}
    char *p1=getpass("Password (minimum 12 characters): ");
    char password[1025];snprintf(password,sizeof(password),"%s",p1?p1:"");
    char *p2=getpass("Repeat password: ");
    if(!p2||strcmp(password,p2)!=0){fprintf(stderr,"Passwords do not match.\n");sodium_memzero(password,sizeof(password));return 1;}
    Auth a={0};if(auth_init(&a,"database/exam.db")!=0){fprintf(stderr,"Cannot open database. Run from project root.\n");sodium_memzero(password,sizeof(password));return 1;}
    int rc=auth_create_user(&a,argv[1],password,argv[2]);auth_close(&a);sodium_memzero(password,sizeof(password));if(rc){fprintf(stderr,"Could not create user (username may exist or password is invalid).\n");return 1;}printf("Created %s account '%s'.\n",argv[2],argv[1]);return 0;
}
