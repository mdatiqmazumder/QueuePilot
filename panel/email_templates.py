"""Self-contained HTML emails with escaped content and inline email-safe styles."""
from html import escape
import json


def render_email(subject, body):
    title = subject.removeprefix('[QueuePilot]').strip()
    lower = title.lower()
    if lower.startswith('failure:'):
        label, color, background, intro = 'ACTION REQUIRED', '#b23c35', '#fff0ed', 'An executor needs your attention. Review its execution logs in QueuePilot.'
    elif lower.startswith('stop:'):
        label, color, background, intro = 'WORKER STOPPED', '#956315', '#fff7e5', 'An executor has stopped. Check the event and restart status below.'
    elif 'health alert' in lower:
        label, color, background, intro = 'HEALTH ALERT', '#b23c35', '#fff0ed', 'Your server health check detected an issue that needs attention.'
    elif 'report' in lower:
        label, color, background, intro = 'DAILY REPORT', '#194d43', '#edf5ed', 'Your execution activity and latest server health, in one place.'
    else:
        label, color, background, intro = 'NOTIFICATION', '#194d43', '#edf5ed', 'An update from your QueuePilot server.'

    parts = []
    for line in body.splitlines():
        if not line.strip():
            continue
        if line.strip() in ('QueuePilot daily report', 'Open the panel for execution details and logs.'):
            continue
        if line == 'Executor status:':
            parts.append('<tr><td colspan="2" style="padding:22px 16px 10px;font-size:13px;font-weight:bold;color:#194d43">EXECUTOR STATUS</td></tr>')
        elif line.startswith('- '):
            parts.append(f'<tr><td colspan="2" style="padding:14px 16px;border-bottom:1px solid #e8ede8;line-height:1.7;overflow-wrap:anywhere">{escape(line[2:])}</td></tr>')
        elif ': ' in line:
            key, value = line.split(': ', 1)
            formatted = escape(value)
            if value.startswith('{'):
                try:
                    metrics = json.loads(value)
                    if isinstance(metrics, dict):
                        formatted = '<br>'.join(f'<strong>{escape(str(k).replace("_", " ").title())}</strong>: {escape(str(v))}' for k, v in metrics.items()) or 'No activity recorded'
                except (ValueError, TypeError):
                    pass
            parts.append(f'<tr><th align="left" valign="top" style="width:32%;padding:13px 16px;border-bottom:1px solid #e8ede8;color:#697a72;font-size:12px;font-weight:normal">{escape(key)}</th><td style="padding:13px 16px;border-bottom:1px solid #e8ede8;line-height:1.6;overflow-wrap:anywhere">{formatted}</td></tr>')
        else:
            parts.append(f'<tr><td colspan="2" style="padding:14px 16px;line-height:1.7;overflow-wrap:anywhere">{escape(line)}</td></tr>')

    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)}</title></head>
<body style="margin:0;padding:0;background:#f3f6f1;color:#243b32;font-family:Arial,Helvetica,sans-serif;font-size:14px">
<div style="display:none;max-height:0;overflow:hidden;mso-hide:all">{escape(title)} — {escape(intro)}</div>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f3f6f1"><tr><td align="center" style="padding:32px 12px">
<table role="presentation" width="600" cellspacing="0" cellpadding="0" style="width:100%;max-width:600px;background:#ffffff;border:1px solid #dfe7de;border-radius:12px;overflow:hidden">
<tr><td style="background:#194d43;padding:24px 28px;color:#ffffff"><span style="display:inline-block;background:#daf27c;color:#194d43;padding:7px 9px;border-radius:7px;font-size:20px;font-weight:bold">qp</span><span style="font-size:22px;font-weight:bold;padding-left:12px">QueuePilot</span><div style="margin-top:12px;font-size:11px;letter-spacing:2px;color:#c6d9cd">SERVER AUTOMATION &amp; MONITORING</div></td></tr>
<tr><td style="padding:28px 28px 20px"><span style="display:inline-block;padding:7px 10px;background:{background};color:{color};border-radius:5px;font-size:10px;font-weight:bold;letter-spacing:1px">{label}</span><h1 style="font-size:24px;line-height:1.3;margin:16px 0 10px;color:#183a30;overflow-wrap:anywhere">{escape(title)}</h1><p style="margin:0;color:#697a72;line-height:1.7">{intro}</p></td></tr>
<tr><td style="padding:0 12px 24px"><table width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;font-size:13px;table-layout:fixed">{''.join(parts)}</table></td></tr>
<tr><td style="padding:20px 28px;background:#fafcf8;border-top:1px solid #e8ede8;color:#697a72;font-size:12px;line-height:1.7">Open your QueuePilot panel to review execution history and server health.<br>Notification preferences are available under <strong>Email &amp; alerts</strong>.</td></tr>
</table><p style="margin:20px 0 0;font-size:11px;color:#859185">Sent by your self-hosted QueuePilot server.</p>
</td></tr></table></body></html>'''
