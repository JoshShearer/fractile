# Runs an Ubuntu 24.04 desktop VM under QEMU/KVM for the real login, logout
# and reboot tests (SRS T10). Test infrastructure only. The container runs as
# an unprivileged user; /dev/kvm is passed in with its group added at run
# time. The VM uses user-mode networking, so nothing on the host network is
# touched.
FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      qemu-system-x86 qemu-utils cloud-image-utils openssh-client curl ca-certificates \
      python3 python3-pil \
    && rm -rf /var/lib/apt/lists/*
# Same uid as the invoking user, so the cache and evidence dirs stay theirs.
ARG UID=1000
RUN (id -u ubuntu >/dev/null 2>&1 && userdel -r ubuntu || true) && useradd -m -u ${UID} runner
USER runner
WORKDIR /home/runner
