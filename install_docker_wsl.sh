#!/usr/bin/env bash
# Install Docker Engine + NVIDIA Container Toolkit inside WSL2 Ubuntu-24.04.
# Must be run as root (e.g. via `wsl -d Ubuntu-24.04 -u root`).
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: this script must run as root. Invoke with:" >&2
  echo "  wsl -d Ubuntu-24.04 -u root -- bash $0" >&2
  exit 1
fi

DOCKER_USER="kakha13"
log() { printf '\n\033[1;36m=== %s ===\033[0m\n' "$*"; }

log "Apt update + prerequisites"
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg lsb-release

log "Add Docker apt repo"
install -m 0755 -d /etc/apt/keyrings
if [ ! -f /etc/apt/keyrings/docker.gpg ]; then
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
fi
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list

log "Install Docker Engine + Compose plugin"
apt-get update -qq
apt-get install -y -qq \
    docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin

log "Add ${DOCKER_USER} to docker group"
usermod -aG docker "${DOCKER_USER}"

log "Enable + start docker via systemd"
systemctl enable --now docker

log "Add NVIDIA Container Toolkit apt repo"
if [ ! -f /etc/apt/keyrings/nvidia-container-toolkit-keyring.gpg ]; then
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | gpg --dearmor -o /etc/apt/keyrings/nvidia-container-toolkit-keyring.gpg
fi
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/etc/apt/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  > /etc/apt/sources.list.d/nvidia-container-toolkit.list

log "Install nvidia-container-toolkit"
apt-get update -qq
apt-get install -y -qq nvidia-container-toolkit

log "Configure dockerd to use nvidia runtime"
nvidia-ctk runtime configure --runtime=docker
systemctl restart docker

log "Versions"
docker --version
docker compose version

log "GPU container smoke test (nvidia-smi inside CUDA container)"
if docker run --rm --runtime=nvidia --gpus all \
    nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi; then
  log "DONE - install successful"
else
  echo "ERROR: GPU smoke test failed. Investigate before running the worker." >&2
  exit 1
fi
