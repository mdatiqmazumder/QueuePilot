"""Allow the configured public panel port through an existing UFW installation."""
import ipaddress
import shutil
import subprocess

from dotenv import dotenv_values


def allow_panel_port():
    values = dotenv_values('.env')
    host = (values.get('PANEL_HOST') or '127.0.0.1').strip()
    port = int(values.get('PANEL_PORT') or '8090')
    if not 1 <= port <= 65535:
        raise ValueError('Invalid panel port')
    try:
        local_only = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local_only = host.lower() == 'localhost'
    if local_only:
        print('Panel listens on localhost; no public firewall rule is needed.')
        return
    ufw = shutil.which('ufw')
    if not ufw:
        print('UFW is not installed; no UFW rule is needed. Check any external firewall separately.')
        return
    try:
        result = subprocess.run([ufw, 'allow', f'{port}/tcp', 'comment', 'QueuePilot panel'],
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            print(f'WARNING: Could not allow TCP port {port}. Run: sudo ufw allow {port}/tcp')
            return
        print(f'UFW rule ready: TCP port {port} allowed for QueuePilot.')
        print('If your provider has a cloud firewall, allow this port there too.')
    except (OSError, subprocess.TimeoutExpired):
        print(f'WARNING: Firewall command failed. Run: sudo ufw allow {port}/tcp')


if __name__ == '__main__':
    allow_panel_port()
