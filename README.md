# Kuma Reports

Scheduled availability and incident reports for [Uptime Kuma](https://github.com/louislam/uptime-kuma),
running as a small sidecar container next to your existing Kuma.

- **Reads Kuma directly, read-only** — monitors, groups, tags, notifications and heartbeats come
  straight from Kuma's database (SQLite, embedded MariaDB or external MariaDB/MySQL; Kuma 1.23+ and 2.x).
  Nothing has to be configured twice.
- **Scope reports the way you organise Kuma** — pick monitors, whole groups (nested, including monitors
  added later), tags (optionally with a tag value, e.g. `customer = acme`), monitor types, exclusions.
- **Mail through what Kuma already has** — reuse Kuma's e-mail notifications (SMTP server, sender and
  recipients), or add your own SMTP transports.
- **One report, many customers** — *split per Kuma notification*: every e-mail notification linked to
  monitors in scope receives its own report with only its monitors.
- **Templates** — four built-in layouts (standard, compact, SLA statement, incident overview), fully
  editable Jinja2 with reusable building blocks, live preview, and translations (EN/NL/DE).
- **Real SLA numbers** — availability from state changes (not just check counts), SLA target,
  MTTR/MTBF, longest incident, business-hours-only calculation, flap filter (ignore incidents shorter
  than N minutes), maintenance excluded or counted, comparison with the previous period.
- **Attachments** — PDF, Excel and CSV. Preview and download everything from the GUI too.
- **Incident notes** — annotate an incident (cause, ticket) or exclude a false alarm from all reports.
- **Own history** — state changes are copied into Kuma Reports' own database every few minutes, so
  monthly/yearly reports keep working after Kuma prunes old heartbeats.

## Quick start

1. Find the name of Kuma's data volume (or its host path):

   ```sh
   docker inspect uptime-kuma --format '{{ range .Mounts }}{{ .Name }}{{ .Source }} -> {{ .Destination }}{{ "\n" }}{{ end }}'
   ```

2. Copy `docker-compose.yml` and `.env.example` (as `.env`), set the volume name, and adjust `.env`.

3. If Kuma uses MariaDB, create a read-only user (see below). For SQLite nothing is needed.

4. `docker compose up -d`, open `http://127.0.0.1:8080`, choose an admin password, and create a report.

The data volume of Kuma is mounted **read-only** at `/kuma`. Kuma Reports detects the database type from
`/kuma/db-config.json`.

### Embedded MariaDB (Kuma 2.x default with MariaDB)

The embedded MariaDB only listens inside the Kuma container; Kuma Reports connects through the socket
in the data volume (`/kuma/run/mariadb.sock`). Create a user that can only read:

```sh
docker exec -it uptime-kuma mariadb -S /app/data/run/mariadb.sock -u root -e "
  CREATE USER 'reporter'@'localhost' IDENTIFIED BY 'choose-a-password';
  GRANT SELECT ON kuma.* TO 'reporter'@'localhost';"
```

and set `KUMA_DB_USER=reporter` / `KUMA_DB_PASSWORD=…` in `.env`.

### External MariaDB / MySQL

Create the same read-only user on your database server (`'reporter'@'%'` or the container's network),
put Kuma Reports on the same Docker network and set `KUMA_DB_HOST`, `KUMA_DB_PORT`, `KUMA_DB_NAME`.
Host, port and database name are also read from Kuma's `db-config.json` when not set.

### SQLite

Nothing to configure: `/kuma/kuma.db` is opened in read-only mode. Kuma Reports never writes to it.

## Configuration

All settings are environment variables; see [`.env.example`](.env.example). Every variable can also be
read from a file with the `_FILE` suffix (Docker secrets), e.g. `KUMA_DB_PASSWORD_FILE`.

| Variable | Default | |
|---|---|---|
| `KUMA_DB_TYPE` | `auto` | `auto`, `sqlite`, `embedded-mariadb`, `mariadb` |
| `KUMA_DB_URL` | | Full SQLAlchemy URL, overrides the settings below |
| `KUMA_DB_USER` / `KUMA_DB_PASSWORD` | `reporter` / – | MariaDB credentials |
| `KUMA_DB_HOST` / `KUMA_DB_PORT` / `KUMA_DB_NAME` | from `db-config.json` | External MariaDB |
| `KUMA_BASE_URL` | | Kuma URL for links in reports |
| `TZ` | `UTC` | Timezone for periods, schedules and dates |
| `AUTH_MODE` | `form` | `form` or `none` (only behind an authenticating proxy) |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / – | Without a password you set one on first visit |
| `BASE_URL` | | Public URL; `https://…` enables secure cookies |
| `IMPORT_INTERVAL` | `300` | Seconds between imports from Kuma |
| `HISTORY_KEEP_DAYS` | `400` | How long archived report mails are kept |
| `PUID` / `PGID` | `1000` | User the app runs as |

## How numbers are calculated

- An **incident** starts at a state change to *down* and ends at the next change to *up* (or
  maintenance). Kuma marks these transitions as "important" heartbeats; Kuma Reports imports exactly those.
- **Availability** = 1 − downtime ÷ monitored time, over the report period. Monitored time starts at the
  monitor's creation if that falls inside the period. Maintenance is removed from both sides, unless
  "count maintenance as downtime" is set. With business hours enabled, only time inside those hours counts.
- **Summary availability** is time-weighted across all monitors in scope.
- **Check counts and response times** come from Kuma's daily statistics (`stat_daily` in 2.x, aggregated
  heartbeats in 1.x).
- Kuma 2.x keeps only a limited number of raw heartbeats per monitor. For history from *before* Kuma
  Reports was installed, a monitor without a known state change is assumed to have been up. From
  installation on, every transition is recorded.

## Templates

Templates are Jinja2 (sandboxed). The built-in ones import building blocks from `macros.html`:

```jinja
{% import "macros.html" as k %}
{% call k.page() %}
  {{ k.header(report, t) }}
  {{ k.tiles(summary, t, sla_target_str, previous) }}
  {{ k.chart(series, series_title) }}
  {{ k.monitor_table(monitors, t) }}
  {{ k.incident_table(incidents, t) }}
  {{ k.footer(report, t) }}
{% endcall %}
```

The template editor lists every available variable and shows a live preview against any report.
Use `t.<label>` for translated labels so the report language applies.

## Security notes

- Kuma Reports only needs read access to Kuma's data. Use the read-only database user; the Kuma volume
  is mounted read-only.
- Kuma's notification settings (including SMTP passwords) are read when sending and are never stored.
- Own SMTP passwords are stored in Kuma Reports' database in `/data` — protect that volume.
- Do not expose the GUI to the internet without a reverse proxy with TLS.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt
DATA_DIR=./data KUMA_DB_TYPE=sqlite KUMA_DB_PATH=/path/to/kuma.db AUTH_MODE=none \
  .venv/bin/uvicorn app.main:app --reload --port 8080
```

PDF output needs WeasyPrint's system libraries (Pango); they are included in the Docker image.

## License

MIT
