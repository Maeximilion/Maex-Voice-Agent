# The server of the load test (#841, docs/13 "Load test"): a Linux host as the
# VPS of D3 runs it, inside one container that api/telephony/load.py starts with
# 4 processors and 8 GB and no swap. systemd, journald (the log driver of
# deploy/docker-compose.prod.yml) and a Docker Engine of its own, so the
# production Compose files run unchanged and the host's own share counts.
# Privileged, because a Docker Engine runs inside it; on the workbench only.
FROM ubuntu:24.04
RUN apt-get update -qq \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
       systemd systemd-sysv dbus docker.io docker-compose-v2 docker-buildx \
       ca-certificates iproute2 python3 tzdata git >/dev/null \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
# The deploy user of docs/13 §3, with the uid of the workbench's user so it
# reads the checkout mounted at /src and writes the run directory at /load.
ARG DEPLOY_UID=1000
ARG DEPLOY_GID=1000
RUN (getent passwd "$DEPLOY_UID" | cut -d: -f1 | xargs -r userdel -r) \
    && (getent group "$DEPLOY_GID" || groupadd -g "$DEPLOY_GID" deploy) \
    && useradd -m -u "$DEPLOY_UID" -g "$DEPLOY_GID" -G docker deploy \
    && git config --system --add safe.directory '*' \
    && mkdir -p /srv/maex /etc/systemd/journald.conf.d \
    && chown deploy /srv/maex
COPY journald.conf /etc/systemd/journald.conf.d/maex.conf
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
