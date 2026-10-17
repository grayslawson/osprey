#!/usr/bin/env bash
# shellcheck disable=SC1090  # community-scripts build.func is fetched at run time
# Upstream may have moved the bootstrap to community-scripts/core; confirm the
# current URL in their contributor guide before submitting.
source <(curl -fsSL "${COMMUNITY_SCRIPTS_CORE_URL:-https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main}/misc/build.func")
# Copyright (c) 2021-2026 community-scripts ORG
# Author: Osprey contributors
# License: MIT | https://github.com/community-scripts/ProxmoxVE/raw/main/LICENSE
# Source: https://github.com/grayslawson/osprey
#
# STATUS: PENDING. This script follows the community-scripts ct/ + install/
# layout so it can be submitted to community-scripts/DevScripts later. It
# needs a published GitHub Release (check_for_gh_release /
# fetch_and_deploy_gh_release) and is not usable before the first tag.
# Run on the Proxmox VE host:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/grayslawson/osprey/main/packaging/proxmox/ct/osprey.sh)"
# Until it is accepted upstream, build.func fetches install/osprey-install.sh
# from community-scripts; see packaging/proxmox/README.md for local testing.

APP="Osprey"
var_tags="${var_tags:-camera;nvr;onvif}"
var_cpu="${var_cpu:-1}"
var_ram="${var_ram:-512}"
var_disk="${var_disk:-4}"
var_os="${var_os:-debian}"
var_version="${var_version:-13}"
var_unprivileged="${var_unprivileged:-1}"

header_info "$APP"
variables
color
catch_errors

function update_script() {
  header_info
  check_container_storage
  check_container_resources
  if [[ ! -d /opt/osprey ]]; then
    msg_error "No ${APP} Installation Found!"
    exit
  fi

  if check_for_gh_release "osprey" "grayslawson/osprey"; then
    msg_info "Stopping Service"
    systemctl stop osprey
    msg_ok "Stopped Service"

    msg_info "Backing up state"
    tar -czf "/root/osprey-state-$(date +%F-%H%M%S).tar.gz" -C / var/lib/osprey etc/osprey
    msg_ok "Backed up state to /root"

    CLEAN_INSTALL=1 fetch_and_deploy_gh_release "osprey" "grayslawson/osprey" "tarball"

    msg_info "Starting Service"
    systemctl start osprey
    msg_ok "Started Service"
    msg_ok "Updated successfully!"
  fi
  exit
}

start
build_container
description

msg_ok "Completed successfully!\n"
echo -e "${CREATING}${GN}${APP} setup has been successfully initialized!${CL}"
echo -e "${INFO}${YW} Open the operator console and finish setup:${CL}"
echo -e "${TAB}${GATEWAY}${BGN}http://${IP}:8000/setup${CL}"
echo -e "${INFO}${YW} Setup token: journalctl -u osprey | grep 'setup token'${CL}"
