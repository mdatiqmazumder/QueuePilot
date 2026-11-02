import os
from dotenv import dotenv_values, set_key


def main():
    original = os.stat('.env')
    values = dotenv_values('.env')
    print('Press Enter to keep settings. Database credentials are not displayed.')
    def ask(label, key, default):
        old = values.get(key, default)
        return input(f'{label} [{old}]: ').strip() or old
    host = ask('Listen address', 'PANEL_HOST', '0.0.0.0')
    port = int(ask('Panel port', 'PANEL_PORT', '8090'))
    slots = int(ask('Concurrent runs', 'MAX_CONCURRENT_RUNS', '8'))
    secure = ask('HTTPS-only cookies true/false', 'COOKIE_SECURE', 'false')
    if not 1 <= port <= 65535 or slots < 1 or secure not in ('true', 'false') or not host or '\n' in host:
        raise SystemExit('Invalid configuration. No settings changed.')
    for key, value in dict(PANEL_HOST=host, PANEL_PORT=str(port), MAX_CONCURRENT_RUNS=str(slots), COOKIE_SECURE=secure).items():
        set_key('.env', key, value)
    if hasattr(os, 'chown'):
        os.chown('.env', original.st_uid, original.st_gid)
    os.chmod('.env', 0o640)
    from .firewall import allow_panel_port
    allow_panel_port()


if __name__ == '__main__':
    main()
