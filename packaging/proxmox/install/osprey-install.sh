#!/usr/bin/env bash
# Copyright (c) 2021-2026 community-scripts ORG
# Author: Osprey contributors
# License: MIT | https://github.com/community-scripts/ProxmoxVE/raw/main/LICENSE
# Source: https://github.com/grayslawson/osprey
#
# STATUS: PENDING until the first GitHub Release exists (see ct/osprey.sh).
# Installs Osprey natively in a Debian 13 LXC from the release source tarball.
# After the Phase 0 Python package lands (specs/002-python-package-cli), this
# switches to `uv tool install osprey-ptz==<version>` and `osprey run`.

# shellcheck disable=SC1090,SC1091  # FUNCTIONS_FILE_PATH is provided by build.func
source /dev/stdin <<<"$FUNCTIONS_FILE_PATH"
color
verb_ip6
catch_errors
setting_up_container
network_check
update_os

msg_info "Installing Dependencies"
$STD apt install -y python3 python3-venv openssl ffmpeg
msg_ok "Installed Dependencies"

fetch_and_deploy_gh_release "osprey" "grayslawson/osprey" "tarball"

msg_info "Setting up Python environment"
$STD python3 -m venv /opt/osprey-venv
$STD /opt/osprey-venv/bin/pip install --no-cache-dir "paho-mqtt==2.1.0"
msg_ok "Set up Python environment"

msg_info "Configuring Osprey"
useradd --system --home-dir /var/lib/osprey --no-create-home --shell /usr/sbin/nologin osprey
install -d -m 0750 -o root -g osprey /etc/osprey /etc/osprey/credentials
LXC_IP=$(hostname -I | awk '{print $1}')
cat <<ENV >/etc/osprey/osprey.env
# Address the camera dials back to (TCP 7442/7444/7550). Must be this LXC's
# LAN address; change it if the container's address changes.
OSPREY_HOST=${LXC_IP}
# Remove after the first-run wizard has saved /var/lib/osprey/osprey.json.
OSPREY_SETUP=1
ENV
openssl rand -base64 24 >/etc/osprey/credentials/admin-password
chmod 0640 /etc/osprey/osprey.env
chmod 0600 /etc/osprey/credentials/admin-password
msg_ok "Configured Osprey"

msg_info "Creating Service"
cat <<'UNIT' >/etc/systemd/system/osprey.service
[Unit]
Description=Osprey PTZ controller
Documentation=https://github.com/grayslawson/osprey
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=osprey
Group=osprey
EnvironmentFile=/etc/osprey/osprey.env
Environment=PATH=/opt/osprey-venv/bin:/usr/bin:/bin
Environment=PYTHONPATH=/opt/osprey/pyunifiwire/src
Environment=OSPREY_STATE_DIR=/var/lib/osprey
Environment=OSPREY_ADMIN_PASSWORD_FILE=%d/admin-password
LoadCredential=admin-password:/etc/osprey/credentials/admin-password
ExecStart=/opt/osprey/docker/entrypoint.sh run
StateDirectory=osprey
StateDirectoryMode=0750
WorkingDirectory=/var/lib/osprey
Restart=on-failure
RestartSec=5s
TimeoutStopSec=15s
NoNewPrivileges=yes
CapabilityBoundingSet=
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
RestrictNamespaces=yes
RestrictSUIDSGID=yes
LockPersonality=yes
SystemCallArchitectures=native
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT
systemctl enable -q --now osprey
msg_ok "Created Service"

motd_ssh
customize
cleanup_lxc
