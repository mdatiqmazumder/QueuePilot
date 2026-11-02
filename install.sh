#!/usr/bin/env bash
set -euo pipefail

main() {
  if [[ $EUID -ne 0 ]]; then echo 'Run this installer with sudo.'; return 1; fi
  source /etc/os-release
  if [[ "$ID" != ubuntu || ${VERSION_ID%%.*} -lt 24 || ${VERSION_ID%%.*} -gt 26 ]]; then
    echo 'Supported: Ubuntu 24.x through 26.x with systemd.'; return 1
  fi
  if [[ $(ps -p 1 -o comm=) != systemd ]]; then
    echo 'systemd required. WSL: enable [boot] systemd=true in /etc/wsl.conf and restart WSL.'; return 1
  fi
  local archive_url='' service_user='' source_dir temp_dir='' install_dir=/opt/queuepilot
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --source) archive_url=${2:?Provide HTTPS source archive}; shift 2 ;;
      --user) service_user=${2:?Provide existing non-root Linux user}; shift 2 ;;
      *) echo "Unknown option: $1"; return 1 ;;
    esac
  done
  if [[ -e "$install_dir/.env" ]]; then echo 'Already installed. Use: queuepilot'; return 1; fi
  if [[ -z "$service_user" ]]; then
    read -r -p 'Existing Linux execution user [www-data]: ' service_user
    service_user=${service_user:-www-data}
  fi
  if [[ ! "$service_user" =~ ^[a-z_][a-z0-9_-]*\$?$ ]] || ! id -- "$service_user" >/dev/null 2>&1 || [[ $(id -u -- "$service_user") == 0 ]]; then
    echo 'Choose an existing non-root user with access to your application directories.'; return 1
  fi
  local service_group
  service_group=$(id -gn -- "$service_user")
  apt-get update
  apt-get install -y python3 python3-venv python3-pip ca-certificates curl
  source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-./install.sh}")" && pwd)"
  if [[ -n "$archive_url" ]]; then
    if [[ "$archive_url" != https://* ]]; then echo '--source requires HTTPS'; return 1; fi
    temp_dir=$(mktemp -d /tmp/queuepilot.XXXXXXXX)
    curl --fail --location --proto '=https' --tlsv1.2 "$archive_url" -o "$temp_dir/source.tar.gz"
    python3 - "$temp_dir" <<'PY'
import pathlib, sys, tarfile
root = pathlib.Path(sys.argv[1])
with tarfile.open(root / 'source.tar.gz') as archive:
    archive.extractall(root / 'source', filter='data')
PY
    source_dir=$(find "$temp_dir/source" -maxdepth 3 -name requirements.txt -exec dirname {} \; | head -n 1)
  fi
  if [[ ! -f "$source_dir/panel/app.py" || ! -f "$source_dir/bin/queuepilot" ]]; then
    echo 'Missing source. Clone repository or pass --source https://host/source.tar.gz'; return 1
  fi
  install -d -m 0755 "$install_dir"
  if [[ "$source_dir" != "$install_dir" ]]; then
    cp -R "$source_dir/panel" "$install_dir/"
    cp "$source_dir/requirements.txt" "$source_dir/README.md" "$install_dir/"
  fi
  install -m 0755 "$source_dir/bin/queuepilot" /usr/local/bin/queuepilot
  cd "$install_dir"
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
  install -d -o "$service_user" -g "$service_group" -m 0700 data
  .venv/bin/python -m panel.setup
  .venv/bin/python -m panel.firewall
  chown root:"$service_group" .env
  chmod 0640 .env
  chown -R "$service_user:$service_group" data
  cat > /etc/systemd/system/queuepilot.service <<EOF
[Unit]
Description=QueuePilot Executor Panel and scheduler
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$service_user
Group=$service_group
WorkingDirectory=$install_dir
ExecStart=$install_dir/.venv/bin/python -m panel.serve
Restart=on-failure
RestartSec=5
KillMode=mixed
TimeoutStopSec=3700
NoNewPrivileges=true
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  /usr/local/bin/queuepilot setup-ui-updates
  if ! systemctl is-active --quiet queuepilot-update.path; then
    echo 'UI update helper failed to start. Inspect: journalctl -u queuepilot-update.path'; return 1
  fi
  systemctl enable --now queuepilot
  sleep 2
  if ! systemctl is-active --quiet queuepilot; then
    echo 'Service failed. Inspect: journalctl -u queuepilot -n 50'; return 1
  fi
  echo 'Installed. Type queuepilot to open the management menu.'
  echo 'UI updates are enabled automatically. Open Updates in the panel.'
  echo 'Health audit and daily report automations are ready. Configure SMTP in the panel under Email & alerts.'
  echo 'The panel port is allowed in UFW when available. Check any provider firewall separately.'
  if [[ -n "$temp_dir" ]]; then rm -rf -- "$temp_dir"; fi
}

# Parse the entire function first, then read interactive answers from the terminal.
if [[ -t 0 ]]; then main "$@"; else main "$@" < /dev/tty; fi
