# QueuePilot

Run Laravel queue workers, schedule scripts, monitor execution health, and receive email reports from one self-hosted control panel.

QueuePilot provides a web dashboard on your chosen port and a `queuepilot` terminal command for managing the service. Configuration and execution history are stored in MySQL.

## Features

- **Continuous workers:** keep Laravel queue workers or other foreground processes running, with automatic restart after exit.
- **Scheduled executors:** run scripts and commands using five-field cron expressions and a selected timezone.
- **Laravel validation:** check the working directory, PHP executable, and readable `artisan` file before saving and running an executor.
- **Execution history:** inspect status, output, duration, exit code, and recent logs; cancel active runs and download output.
- **Server health:** view CPU, memory, disk usage, database connectivity, scheduler heartbeat, and executor health audits.
- **Email notifications:** configure SMTP, receive failure/stop/health alerts, and schedule daily reports.
- **Editable system automations:** health audit and daily report jobs are created automatically and can be edited or paused.
- **No fixed executor count:** pagination supports growing lists; simultaneous execution is limited by configurable slots and server resources.

## Requirements

- Ubuntu 24.x, 25.x, or 26.x with systemd; the installer has been tested on Ubuntu 24.04 under WSL2.
- Python 3.12 or newer. The installer installs Python and creates a virtual environment.
- An existing MySQL database and a user with permissions to create tables and indexes, alter tables for migrations, and read/write application records.
- A non-root Linux account with access to your application directories. The default execution account is `www-data`.
- PHP CLI and your Laravel application dependencies when using Laravel executors. QueuePilot does not install your Laravel application.

Use a dedicated database for QueuePilot. Its database credentials and web admin login are separate from your Laravel application's configuration.

## Install

Install directly from this repository:

```bash
curl -fsSL https://raw.githubusercontent.com/mdatiqmazumder/QueuePilot/main/install.sh | sudo bash -s -- --source https://github.com/mdatiqmazumder/QueuePilot/archive/refs/heads/main.tar.gz
```

If `curl` is missing, install it first with `sudo apt-get update && sudo apt-get install -y curl`.

Or clone the repository and run the installer:

```bash
git clone https://github.com/mdatiqmazumder/QueuePilot.git queuepilot
cd queuepilot
sudo bash install.sh
```

The installer asks for:

1. The Linux user that will run your scripts.
2. Server IP/hostname, panel listen address, and panel port.
3. MySQL host, port, username, password, and database name.
4. Admin username and a password of at least 12 characters.
5. Maximum concurrent executions and whether cookies require HTTPS.

An empty MySQL password is accepted when permitted by your database server. The database must already exist. The installer creates QueuePilot's tables, enables its systemd service, and creates the built-in automations.

Open `http://SERVER_IP:PANEL_PORT` and sign in with the admin credentials you selected.

| Installed component | Location / name |
| --- | --- |
| Application | `/opt/queuepilot` |
| Configuration | `/opt/queuepilot/.env` |
| Runtime data | `/opt/queuepilot/data` |
| Service | `queuepilot.service` |
| Command | `/usr/local/bin/queuepilot` |

### Hosting the installer yourself

Upload `install.sh` and the source archive to your HTTPS server:

```bash
curl -fsSL https://example.com/queuepilot/install.sh | sudo bash -s -- --source https://example.com/queuepilot/queuepilot.tar.gz
```

Run `python tools/package.py` to generate `dist/queuepilot.tar.gz` and `dist/queuepilot-github.zip`. Packages exclude local credentials, databases, runtime data, and virtual environments. For GitHub upload, extract the ZIP and upload its contents so that `install.sh` is at the repository root. GitHub generates the `main.tar.gz` branch archive automatically.

## Terminal management

Run `queuepilot` to open the interactive menu, or use a command directly:

```bash
queuepilot start
queuepilot stop
queuepilot restart
queuepilot status
queuepilot logs       # Last 100 service log entries
queuepilot follow     # Follow logs; Ctrl+C to exit
queuepilot settings   # Change port, bind address, concurrency, or HTTPS cookies
queuepilot password   # Reset an admin password and revoke existing sessions
queuepilot doctor    # Check database access, configuration, and PHP availability
queuepilot update    # Download updates, back up, and restart the panel
queuepilot enable    # Start the service at boot
queuepilot disable
```

The command uses sudo when necessary. Settings changes restart the service. To change database credentials, edit `/opt/queuepilot/.env` as root and restart QueuePilot. The configuration file is readable only by root and the service group (mode 0640).

## Continuous Laravel workers

Create an executor with **Type: Laravel**, **Laravel task: Queue worker**, and your application's absolute working directory, such as `/var/www/my-app`. Fill in the PHP executable, queue names, idle sleep, maximum attempts, job timeout, and optional worker recycling interval. QueuePilot builds the command and selects continuous mode automatically; a command preview shows what will run.

For additional Artisan options, select **Custom Artisan command · advanced** and enter arguments as a JSON array. Existing commands with extra or unsupported options remain in the advanced editor so they are preserved:

```json
["php", "artisan", "queue:work", "--sleep=3", "--tries=3", "--timeout=60"]
```

Enable **Start automatically and keep running** and save. The worker stays running without a process lifetime timeout. After it exits, QueuePilot starts it again after the restart delay (default: 5 seconds), including after a clean exit. Enabled workers also start when the panel starts.

Use **Start worker** and **Stop worker** to control the process. Stop disables automatic restart and sends SIGTERM. QueuePilot allows the current job to finish within the graceful stop wait (default: 90 seconds), then forcefully terminates the process group if necessary. Set this wait above your longest job duration, up to 3600 seconds. Stop an active worker before editing its configuration.

Laravel's `--timeout=60` limits an individual job, not the worker's lifetime. Set the queue connection's `retry_after` above that timeout, such as 90 seconds. Do not use `--stop-when-empty` or `--once` for a worker intended to stay running. Your application must use a background queue connection such as database or Redis instead of `sync`.

After deploying Laravel code, `php artisan queue:restart` asks the worker to exit after its current job; QueuePilot then starts it with the updated code. See the [Laravel queue documentation](https://laravel.com/framework/docs/queues).

For multiple workers, create separate continuous executors with different names. Each worker occupies one execution slot. If every slot is occupied by continuous workers, scheduled executors wait in the queue. Adjust the limit using `queuepilot settings`.

## Scheduled scripts and commands

For Laravel's scheduler, select **Laravel task: Scheduler · every minute** and enter the application directory. QueuePilot builds `php artisan schedule:run` and sets the schedule to `* * * * *`. Output is available in execution history. Remove any existing equivalent crontab entry before enabling this executor to avoid duplicate execution. You can adjust the schedule if needed.

Choose **Execution mode: Scheduled** and supply a five-field cron expression, timezone, working directory, and command arguments.

| Purpose | Command arguments | Schedule |
| --- | --- | --- |
| Laravel scheduler | `["php", "artisan", "schedule:run"]` | `* * * * *` |
| Python report | `["python3", "daily_report.py"]` | `0 9 * * *` |
| Bash script | `["/bin/bash", "/opt/scripts/backup.sh"]` | `30 2 * * *` |
| Every five minutes | Your executable and arguments | `*/5 * * * *` |

The default timezone is `Asia/Dhaka`; you can change it per executor. Shell pipes and redirection are not parsed. Put shell logic in a script and run that script with Bash.

QueuePilot checks paths and executable permissions before saving and immediately before execution. When an executor already has a queued or running execution, subsequent scheduled slots are skipped. Downtime does not create a catch-up backlog: an overdue executor runs once, then receives its next future slot. Pausing stops future scheduling; cancel an existing run separately.

Scheduled runs have a process timeout. Failed scheduled runs are not automatically retried; inspect their side effects before running them again. Continuous worker restarts are separate from Laravel's own job retry policy.

## SMTP notifications and reports

## Telegram bot

Open **Telegram bot** in the sidebar. The settings page includes the complete setup guide and command reference. Create a dedicated bot with **@BotFather** using `/newbot`, open the new bot and send `/start`, then find `message.chat.id` and `message.from.id` using Telegram's `getUpdates` API before enabling polling. Keep your bot token private. Enter the token and Chat ID, enable Telegram, save, and send a test message.

Enable bot commands and set **Allowed Telegram user ID** to authorize remote control. Both chat ID and sender user ID must match. Commands: `/help`, `/status`, `/jobs`, `/run ID`, `/stop ID`. Stop applies to continuous workers and disables automatic restart. Run queues an execution, or starts a continuous worker. Old commands are skipped after authorization changes; commands older than two minutes are ignored.

Send `/start` or `/help` to open the button menu. **Status**, **Executors**, and **Help** are available as buttons; **Executors** shows Start/Stop buttons for the first 20 executors. Typed commands support additional IDs. Button callbacks enforce the same chat and user authorization as typed commands.

Failure, stop, health, and daily-report notifications work independently of SMTP. Delivery history shows attempts and sanitized errors. Failed sends retry up to three times. Long reports are shortened in Telegram; the full report is available in the system automation's execution history. Telegram requires outbound HTTPS and uses polling; use one dedicated bot per panel, remove existing webhooks, and avoid other polling clients. Tokens are encrypted with `data/smtp.key`; include this file in backups. A server or panel that is offline cannot send notifications. A crash after Telegram accepts a message may cause a duplicate on retry.

See the [official Telegram Bot API](https://core.telegram.org/bots/api) for bot creation, `getUpdates`, and `deleteWebhook` details.

## Email settings

Open **Email & alerts**, enter your SMTP host, port, security mode, username/password, sender, and recipients, then enable email delivery and save. Use **Send test email** and inspect the delivery history.

Emails include a branded HTML layout with status badges and readable details for failure, stop, health, and daily report messages. A plain-text alternative is included for mail clients that do not display HTML. No external images are required.

- STARTTLS and SSL/TLS verify certificates and do not fall back to an unencrypted connection.
- **None** is available for a trusted local SMTP relay.
- Leaving the password blank keeps the saved password; **Remove saved password** clears it.
- Failure/timeout, stop/interruption, and health notifications can be enabled separately.
- Manual Stop, Cancel, and Pause events also generate alerts when their notification category is enabled.
- Successful continuous worker exits (exit code 0), including Laravel's `--max-time` recycling and `queue:restart`, restart normally without sending a stop email. Nonzero exits still generate failure alerts.
- Repeat alerts for the same executor/event use a configurable cooldown, defaulting to 15 minutes.

Messages are stored in a durable outbox and processed independently of executor slots. Failed delivery is retried up to three attempts, with delays of 60 and 300 seconds between attempts. Delivery history shows pending, sending, sent, failed, or cancelled status, attempts, and sanitized SMTP error details. **Sent** means the SMTP server accepted the message; it does not guarantee inbox delivery. Saving SMTP configuration cancels older pending mail. A crash immediately after SMTP acceptance may cause a duplicate on recovery with the same Message-ID.

Passwords are encrypted in the database. The encryption key is `/opt/queuepilot/data/smtp.key`, with Linux mode 0600. Back up this key with the database; without it, enter the SMTP password again. The API never returns the stored password.

### Built-in automations

| Automation | Default schedule | Behavior |
| --- | --- | --- |
| Health audit | Every minute: `* * * * *` | Checks enabled executors, paths/artisan, tracked processes, excessive queue wait, overdue runs, and CPU/memory/disk thresholds. |
| Daily email report | 09:00 in `Asia/Dhaka`: `0 9 * * *` | Reports the previous 24 hours of execution totals, executor status, latest run details, and the most recent health audit. Skips email when SMTP is disabled. |

Both appear as **System automation** cards under **Executors**. Edit the name, cron expression, timezone, enable state, or health thresholds. You can run either immediately and inspect its history. Changes survive restarts and updates. Pause built-in automations instead of deleting them.

A reserved system execution slot allows these automations to run even when all normal slots are occupied. **Server health** shows the latest audit results and email outbox status.

Health audits inspect processes and configuration; they do not prove that a running application is making business-level progress. When the entire panel, server, or database is unavailable, it cannot send alerts itself. Use an external monitor for full host outage detection.

## Execution, access, and storage

Executors run as the Linux account selected during installation. Give that account the necessary access to your project, including Laravel's `storage/` and `bootstrap/cache/` directories. QueuePilot does not switch Linux users per executor. Only trusted administrators should have access to this command-execution panel.

The default concurrency is 8. Lists and history are paginated in groups of 50. Resource capacity depends on CPU, memory, disk, and MySQL performance; there is no fixed executor record limit.

Each run stores the last 256 KiB of combined stdout/stderr in MySQL. Scheduled timeouts kill the Linux process group; manual Stop/Cancel first attempts graceful termination. Interrupted running executions are recorded when the panel recovers. Commands must remain in the foreground rather than daemonizing themselves.

Run one panel process and one Uvicorn worker per installation. The data-directory lock prevents duplicate local processes. Sharing one QueuePilot database across several active panel servers is not supported.

## HTTPS deployment

For remote access, put QueuePilot behind an HTTPS reverse proxy, bind it to `127.0.0.1`, and enable HTTPS-only cookies using `queuepilot settings`. Keep HTTPS-only cookies disabled when accessing the panel over HTTP.

Example Nginx location inside a server configured with your TLS certificate:

```nginx
location / {
    proxy_pass http://127.0.0.1:8090;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

Trusted proxy mode is not enabled; login rate limiting uses the proxy's IP address when a reverse proxy is present.

## WSL development

Enable systemd in your WSL distribution and open an Ubuntu terminal before using the panel. WSL can stop when no foreground session remains, even if a systemd service is enabled. Keep the Ubuntu terminal or QueuePilot menu open during use.

Use `http://127.0.0.1:8090` in the Windows browser. On some WSL network configurations, `localhost` resolves to IPv6 while the panel listens on IPv4.

## Development and tests

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

Tests use a temporary SQLite database and include local SMTP capture. Production uses MySQL, so deployed MySQL/systemd smoke tests are separate. Initialize a local configuration with `python -m panel.setup`, then run `python -m panel.serve`.

Windows supports API/UI development; the production installer, systemd integration, and process-group lifecycle are intended for Linux.

## Backup and updates

Back up the MySQL database, `/opt/queuepilot/.env`, and `data/smtp.key`. Execution and email history live in MySQL. There is no automatic log-retention policy.

Run `queuepilot update` to download the latest GitHub source, back up the installation, install requirements, and restart the panel. Settings and `data/` are preserved. A failed update restores the installation backup and attempts to start the panel again. Backups are kept beside `/opt/queuepilot`; they do not include a MySQL database backup. Additive schema migrations run at startup, and built-in automation settings are preserved.

The **Updates** page also supports **Check for updates** and **Update now**. A red dot appears beside Updates when GitHub has a newer commit. Checks are cached for five minutes. Installations without revision tracking offer an update once to establish the installed revision. Existing installations must run `queuepilot update` once in the terminal to install the UI update helper. New installations configure it automatically. The helper uses a fixed systemd path and service; the panel retains `NoNewPrivileges` and does not receive sudo access. Update progress survives the panel restart. Inspect failures with `journalctl -u queuepilot-update -n 100`.

The systemd unit uses `KillMode=mixed` and `TimeoutStopSec=3700` so the panel can gracefully stop its workers before systemd performs final process-group cleanup.
