# QueuePilot verification

Environment: Ubuntu 24.04 under WSL2, Python 3.12.3, systemd, and MySQL 8.0.30 on the Windows host.

## Automated checks

- The integration suite has 25 tests covering authentication, CSRF, path/artisan validation, output and exit codes, overlap protection, scheduling, cancellation, continuous workers, graceful termination, forced termination, and additive migrations.
- SMTP tests cover encrypted password storage and redaction, transport/authentication selection, real loopback SMTP delivery, history, retries, cooldowns, notification preferences, and cancellation of pending mail when delivery is disabled.
- System automation tests verify default seeding, preservation of edited settings, the reserved execution slot, daily reports, and health alerts for missing paths.
- JavaScript and Bash syntax are checked separately.

## Deployment checks

- The interactive installer and CLI service controls have been tested on Ubuntu 24.04.
- A real MySQL/systemd smoke test verifies continuous workers survive beyond scheduled timeouts, resume after service restart, and stay stopped after an explicit Stop request.
- A loopback SMTP server has captured test, failure, stop, report, and health messages from the deployed panel. Delivery history is persisted in MySQL.
- Browser checks cover login, executor creation, logs, SMTP Save, editable system automations, and the automation list.

## QueuePilot branding and English release

- The README, web interface, CLI menu, installer, service paths, SMTP messages, and package names use QueuePilot and English text.
- All 25 integration tests passed after the rename. JavaScript, Python, and Bash syntax checks passed.
- The WSL installation was migrated to `/opt/queuepilot`, `queuepilot.service`, and the `queuepilot` command while preserving its existing database and runtime settings.
- Live checks confirmed the English HTML page, renamed login cookie, database health, continuous-worker recovery, and all five SMTP notification categories.
- The SMTP smoke test isolates newly created delivery records and uses a unique health fixture so repeated verification does not reuse earlier mail records or alert cooldowns.
- The configured GitHub repository is `mdatiqmazumder/QueuePilot`; installation URLs use that repository location.

## Scope and limits

- Ubuntu 25/26 are accepted by the installer but have not been tested here.
- A complete Laravel application was not installed; artisan validation and documented queue-worker behavior are covered separately from an actual application queue.
- External SMTP credentials and real recipients must be configured by the owner. Loopback SMTP acceptance does not verify a third-party provider or inbox delivery.
- Tests emit an upstream Starlette/httpx deprecation warning but no failures.
- WSL must remain running while using the panel; an enabled systemd service alone does not keep the distribution alive.
