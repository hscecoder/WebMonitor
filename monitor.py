import csv
import json
import os
import ssl
import smtplib
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


CONFIG_PATH = Path("config/sites.json")
LOG_PATH = Path("logs/uptime_log.csv")


@dataclass
class CheckResult:
    timestamp_utc: str
    name: str
    url: str
    expected_status: int
    actual_status: int
    latency_ms: int
    is_up: bool
    error: str

    def to_row(self) -> List[str]:
        return [
            self.timestamp_utc,
            self.name,
            self.url,
            str(self.expected_status),
            str(self.actual_status),
            str(self.latency_ms),
            "1" if self.is_up else "0",
            self.error,
        ]


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_sites(path: Path = CONFIG_PATH) -> List[Dict]:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    sites = data.get("sites", [])
    if not sites:
        raise ValueError("No sites configured in config/sites.json")
    return sites


def check_site(site: Dict) -> CheckResult:
    name = site["name"]
    url = site["url"]
    expected_status = int(site.get("expected_status", 200))
    timeout_seconds = int(site.get("timeout_seconds", 10))
    keyword = site.get("keyword")

    start = time.perf_counter()
    actual_status = 0
    body_text = ""
    error = ""
    is_up = False

    try:
        req = Request(url, headers={"User-Agent": "health-monitor/1.0"})
        with urlopen(req, timeout=timeout_seconds) as response:
            actual_status = int(response.getcode())
            body = response.read()
            body_text = body.decode("utf-8", errors="replace")

        status_ok = actual_status == expected_status
        keyword_ok = True if not keyword else (keyword in body_text)
        is_up = status_ok and keyword_ok
        if not status_ok:
            error = f"Expected {expected_status}, got {actual_status}"
        elif not keyword_ok:
            error = f"Keyword '{keyword}' not found"
    except HTTPError as exc:
        actual_status = int(exc.code) if exc.code else 0
        error = f"HTTPError: {exc}"
    except URLError as exc:
        error = f"URLError: {exc}"
    except Exception as exc:  # noqa: BLE001
        error = f"Exception: {exc}"

    latency_ms = int((time.perf_counter() - start) * 1000)
    return CheckResult(
        timestamp_utc=now_utc_iso(),
        name=name,
        url=url,
        expected_status=expected_status,
        actual_status=actual_status,
        latency_ms=latency_ms,
        is_up=is_up,
        error=error,
    )


def ensure_log_file(path: Path = LOG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "timestamp_utc",
                "name",
                "url",
                "expected_status",
                "actual_status",
                "latency_ms",
                "is_up",
                "error",
            ]
        )


def append_results(results: List[CheckResult], path: Path = LOG_PATH) -> None:
    ensure_log_file(path)
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        for result in results:
            writer.writerow(result.to_row())


def read_last_status_by_name(path: Path = LOG_PATH) -> Dict[str, bool]:
    if not path.exists():
        return {}
    last: Dict[str, bool] = {}
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            last[row["name"]] = row["is_up"] == "1"
    return last


def state_change_messages(previous: Dict[str, bool], current: List[CheckResult]) -> List[str]:
    messages = []
    for result in current:
        prev = previous.get(result.name)
        # Always alert when target is down (exam/demo friendly behavior).
        if not result.is_up:
            messages.append(
                f"DOWN: {result.name} | {result.url} | latency={result.latency_ms}ms | error={result.error or 'N/A'}"
            )
            continue

        # Recovery alert only when previous state is known and was down.
        if (prev is False) and result.is_up:
            messages.append(
                f"RECOVERED: {result.name} | {result.url} | latency={result.latency_ms}ms"
            )
    return messages


def send_webhook_alert(message: str) -> Optional[str]:
    webhook_url = os.getenv("ALERT_WEBHOOK_URL")
    if not webhook_url:
        return None
    body = json.dumps({"text": message}).encode("utf-8")
    req = Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=10):
            return None
    except Exception as exc:  # noqa: BLE001
        return f"Webhook alert failed: {exc}"


def send_email_alert(subject: str, message: str) -> Optional[str]:
    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = os.getenv("SMTP_PORT")
    smtp_username = os.getenv("SMTP_USERNAME")
    smtp_password = os.getenv("SMTP_PASSWORD")
    alert_from = os.getenv("ALERT_FROM_EMAIL")
    alert_to = os.getenv("ALERT_TO_EMAIL")

    required = [smtp_host, smtp_port, smtp_username, smtp_password, alert_from, alert_to]
    if not all(required):
        return None

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = alert_from
    msg["To"] = alert_to
    msg.set_content(message)

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(smtp_host, int(smtp_port), timeout=15) as server:
            server.starttls(context=context)
            server.login(smtp_username, smtp_password)
            server.send_message(msg)
        return None
    except Exception as exc:  # noqa: BLE001
        return f"Email alert failed: {exc}"


def main() -> int:
    sites = load_sites()
    previous_status = read_last_status_by_name()

    results = [check_site(site) for site in sites]
    append_results(results)

    down_count = 0
    for result in results:
        label = "UP" if result.is_up else "DOWN"
        print(
            f"[{label}] {result.name} | status={result.actual_status} "
            f"| latency={result.latency_ms}ms | error={result.error or 'None'}"
        )
        if not result.is_up:
            down_count += 1

    changes = state_change_messages(previous_status, results)
    if changes:
        joined = "\n".join(changes)
        webhook_error = send_webhook_alert(joined)
        email_error = send_email_alert("Website Health Monitor Alert", joined)
        if webhook_error:
            print(webhook_error)
        if email_error:
            print(email_error)
    else:
        if not os.getenv("ALERT_WEBHOOK_URL") and not os.getenv("SMTP_HOST"):
            print("No alert channel configured. Set ALERT_WEBHOOK_URL or SMTP_* secrets.")

    return 1 if down_count > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
