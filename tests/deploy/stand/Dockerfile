# A stand-in for the deployment server (PRESET-09a acceptance, run-matrix.py):
# systemd as PID 1 (user units, linger), sshd, its own Docker engine, rsync,
# python3, git (the server's clone) — what scripts/deploy.sh and
# deploy-fence.sh use on 192.168.55.40.
# Run privileged, with /var/lib/docker and /var/lib/containerd on volumes
# (overlay on overlay does not mount).
FROM fedora:42
RUN dnf -y install systemd openssh-server dnf-plugins-core python3 curl tar gzip \
        procps-ng findutils rsync git-core \
 && dnf config-manager addrepo --from-repofile=https://download.docker.com/linux/fedora/docker-ce.repo \
 && dnf -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin \
        docker-compose-plugin \
 && dnf clean all
# The host firewall is nftables; the nested engine must not need iptables.
RUN mkdir -p /etc/docker && echo '{"firewall-backend":"nftables"}' > /etc/docker/daemon.json
RUN useradd -m -u 1000 -G docker jb && mkdir -p /home/jb/.ssh /home/jb/bin /var/lib/systemd/linger \
 && touch /var/lib/systemd/linger/jb && systemctl enable sshd docker && ssh-keygen -A
COPY compose-fault systemd-run /home/jb/bin/
ARG SSH_KEY
RUN echo "$SSH_KEY" > /home/jb/.ssh/authorized_keys \
 && chown -R jb:jb /home/jb && chmod 700 /home/jb/.ssh && chmod 600 /home/jb/.ssh/authorized_keys \
 && chmod 755 /home/jb/bin/compose-fault /home/jb/bin/systemd-run
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
