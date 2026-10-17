# Changelog

## 0.1.3 — 2026-10-17

Found while installing on a small VPS (1 GB RAM, no swap) next to Uptime Kuma:

- Much lower memory use: PDFs are rendered in a short-lived child process, and freed memory is handed back to
  the OS after requests and jobs. Idle ~65 MB (was ~95), peak during a PDF ~135 MB (was ~175), after a PDF
  ~80 MB (was ~173).
- Installer: memory check with a container `mem_limit` (256 MB below 2 GB RAM, else 512 MB) so Kuma Reports can
  never starve Kuma; warns when little memory is available and no swap exists.
- Installer: `--base-path` (with a ready-to-paste nginx block that allows your current SSH client IP) and
  `--kuma-url` for unattended installs.

## 0.1.2 — 2026-10-17

- `BASE_PATH` setting: serve the GUI under a sub-path such as `https://kuma.example.com/reports`, e.g. behind
  Uptime Kuma's own nginx site. `/healthz` keeps working without the prefix; the session cookie is scoped to the path.

## 0.1.1 — 2026-10-17

- `/healthz` is now a real health check (Kuma database, import freshness, scheduler, deliveries in the last
  24 h) with `ok` / `degraded` / `fail` and HTTP 503 on failure — ready to be monitored by Uptime Kuma itself.
- The installer always joins Kuma's Docker network(s), so Kuma can monitor `http://kuma-reports:8080/healthz`.
- 5 s connect timeout for MariaDB/MySQL.

## 0.1.0 — 2026-10-17

First public release.

- Read-only access to Uptime Kuma 1.23+ and 2.x: SQLite, embedded MariaDB (socket) and external MariaDB/MySQL.
- Incremental import of state changes and daily statistics into an own database.
- Reports scoped by monitors, groups (nested), tags (with values), types and exclusions.
- Periods: last 24 h / 7 / 30 / N days, yesterday, this/previous week, month, quarter, year, fixed dates.
- Availability, SLA target, MTTR, MTBF, comparison with the previous period, business hours, flap filter,
  maintenance handling, incident notes and exclusions.
- Delivery via Kuma's e-mail notifications or own SMTP transports; split per Kuma notification;
  only-if-incidents; cron schedules; PDF, Excel and CSV attachments; archive of every delivery.
- Four built-in, editable Jinja2 templates with building blocks and live preview; EN/NL/DE.
- Installer that detects Kuma, its volume and database type, and creates a read-only database user.
