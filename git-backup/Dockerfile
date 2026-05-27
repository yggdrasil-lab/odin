FROM alpine:3.21

RUN apk add --no-cache git openssh-client

COPY git-backup.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/git-backup.sh
