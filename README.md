# Daily Website Health Monitoring with CI/CD

A lightweight DevOps project that checks website/API uptime on schedule, logs history, and sends failure alerts.

## Features

- Scheduled health checks using GitHub Actions (`every 15 minutes`)
- Manual run support (`workflow_dispatch`)
- Uptime history logging in CSV
- Alerting on status change (Down and Recovered):
  - Slack/Teams via webhook
- Daily uptime report generation
- Basic CI tests for quality

## How It Works

1. `monitor.py` reads targets from `config/sites.json`
2. It checks each endpoint (HTTP status, latency, optional keyword)
3. It appends results to `logs/uptime_log.csv`
4. It detects status changes from previous logs:
   - Up -> Down: sends failure alert
   - Down -> Up: sends recovery alert
5. `report.py` builds `logs/daily_report.md` summary
