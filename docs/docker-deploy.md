# VCF Billing Portal — Docker Installation Guide (Ubuntu)

This is the recommended installation method starting with v4.0. It replaces
the old venv + systemd single-process setup (still available as a fallback
in `legacy/legacy-ubuntu-deploy.md`) with a Docker Compose stack of five
containers, so any one component can be rebuilt and redeployed without
touching the others.

## Who this is for

Anyone doing a fresh install of the VCF Billing Portal on an Ubuntu server,
or upgrading an existing v3.x (SQLite / systemd) install to v4.0.

## What you get

| Service     | Role                                                              |
|-------------|--------------------------------------------------------------------|
| `db`        | PostgreSQL 16 — persistent data store                              |
| `migrate`   | One-shot: creates the schema and the default admin account, then exits |
| `api`       | FastAPI app (billing, admin API, PDF statements)                   |
| `collector` | Background loop that polls VCF Operations every N minutes          |
| `frontend`  | Nginx — serves the static UI and reverse-proxies `/api/*`          |

Only `db`'s data (the `db_data` volume) is state that must survive a
redeploy. `api`, `collector`, and `frontend` are stateless and can each be
rebuilt independently.

## Prerequisites

- Ubuntu 22.04 LTS or newer, with a sudo-capable account.
- Outbound internet access (to pull base images from Docker Hub and Ubuntu
  packages from apt).
- A DNS name pointing at this server, if you plan to expose it over HTTPS
  (step 6).

## 0. What changed from v3.x

- **One process → 5 containers.** Previously, changing anything (billing
  logic, the collector, or the UI) meant redeploying the whole codebase and
  restarting one systemd unit that ran both the web server and the
  collector together. Now, for example, if you only changed the collector,
  `docker compose up -d --build collector` is enough — the API and UI are
  never interrupted.
- **SQLite → PostgreSQL.** The old setup pinned a single SQLite file behind
  exactly one uvicorn worker. Splitting into multiple containers (and
  running the API with multiple workers) requires concurrent access, so the
  database moved to PostgreSQL. If you have existing SQLite data, see
  "Migrating existing data" below.
- **TLS termination is unchanged.** The host's nginx + certbot setup still
  terminates TLS exactly as before. The only difference is that nginx now
  proxies to the `frontend` container (`127.0.0.1:8080`) instead of directly
  to uvicorn (`127.0.0.1:8000`) — your existing certificate and renewal hook
  are unaffected.

## 1. Install Docker

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-plugin git
sudo systemctl enable --now docker

# Add your account to the docker group so you don't need sudo for every
# docker command (requires logging out and back in to take effect)
sudo usermod -aG docker "$USER"
```

Confirm the install:

```bash
docker version
docker compose version
```

## 2. Get the source

v4.0 ships as a Git repository instead of a zip file, so future code changes
can be pulled and rebuilt per service.

```bash
sudo mkdir -p /opt/vcf-billing-portal
sudo chown "$USER":"$USER" /opt/vcf-billing-portal
git clone <path or URL to the repository> /opt/vcf-billing-portal
cd /opt/vcf-billing-portal

# If you don't have a remote Git server yet, unpacking the delivered
# archive (it includes the .git directory) is enough to get git log/git
# pull working locally. Connect a remote whenever you're ready:
#   git remote add origin <your git server URL>
#   git push -u origin main
```

## 3. Configure the environment

```bash
cd /opt/vcf-billing-portal
cp .env.example .env
nano .env
```

At minimum, change these values (see the comments in `.env.example` for
details on every setting):

- `SECRET_KEY` — a random value from `openssl rand -hex 32`. **Never change
  this once the service is running** — it's also used to encrypt stored
  integration-account credentials.
- `POSTGRES_PASSWORD` — replace with a strong password.
- `DATABASE_URL` — must match `POSTGRES_PASSWORD` above exactly (the
  password is embedded directly in the URL).

Everything else (`COLLECTOR_INTERVAL_MINUTES`, `COLLECT_USAGE_METRICS`,
`ACCESS_TOKEN_EXPIRE_MINUTES`, `POSTGRES_USER`, `POSTGRES_DB`) can be left at
its default.

## 4. Start the stack

```bash
cd /opt/vcf-billing-portal
docker compose up -d --build
docker compose ps
```

`migrate` is expected to run once, create the schema and default admin
account, and exit cleanly (`Exit 0`) — it is not meant to stay running.
`db`, `api`, `collector`, and `frontend` should all show `healthy` or
`running`.

```bash
docker compose logs -f api collector      # watch startup logs
curl -s http://127.0.0.1:8080/api/health  # expect {"status":"ok"}
```

## 5. (Optional) Hand ownership to a dedicated system account

Docker Compose itself typically runs as root or a member of the `docker`
group; the checked-out repository can stay owned by your own account. On a
shared server, though, it's still good practice to move it to a dedicated,
unprivileged account, the same way the legacy venv install does — see
`legacy/legacy-ubuntu-deploy.md`, step 7, for the reasoning.

## 6. Expose it over nginx + TLS

Use `nginx-vcf-billing.conf` — same filename as the v3.x guide, but the
content now proxies to `127.0.0.1:8080` instead of `:8000`. Replace
`billing.example.com` with your real domain.

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo cp nginx-vcf-billing.conf /etc/nginx/sites-available/vcf-billing-portal
sudo ln -s /etc/nginx/sites-available/vcf-billing-portal /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

sudo certbot --nginx -d billing.example.com
sudo ufw allow 'Nginx Full'
```

If you're upgrading a server that already has a v3.x certificate for the
same domain, you only need to change the `proxy_pass` line in
`nginx-vcf-billing.conf` from `:8000` to `:8080` and reload nginx — no need
to reissue the certificate.

## 7. Final verification

Open `https://billing.example.com`, log in as `admin` / `admin1!2@3#`, and
confirm the dashboard renders. From here, changing the admin password,
registering an integration account, and creating tenants/projects follow
the same steps as README.md's "Integration accounts" and "Tenant / project
management" sections.

## 8. Migrating existing SQLite data (upgrade from v3.x only)

Skip this step for a fresh install. If you're moving an existing v3.x server
(`/opt/vcf-billing-portal/data/billing.db`) to v4.0:

```bash
# 1) Copy the SQLite file from the old server to the new one
scp <old-server>:/opt/vcf-billing-portal/data/billing.db /opt/vcf-billing-portal/data/billing.db

# 2) Start PostgreSQL and prepare the schema first
cd /opt/vcf-billing-portal
docker compose up -d db
docker compose run --rm migrate

# 3) Run the migration script (see the docstring at the top of
#    scripts/migrate_sqlite_to_postgres.py for details)
docker compose run --rm -v "$(pwd)/data:/app/data:ro" api \
    python scripts/migrate_sqlite_to_postgres.py --sqlite-path /app/data/billing.db

# 4) Start everything else
docker compose up -d
```

After migrating, verify that logins, tenants, projects, and VM listings all
match the old server, then stop and disable the old v3.x systemd service:

```bash
sudo systemctl disable --now vcf-billing-portal
```

## 9. Redeploying individual services (the whole point of this change)

```bash
git pull                                   # pull new code
docker compose up -d --build api           # API changed (routing, billing, PDF)
docker compose up -d --build collector     # collector logic changed
docker compose up -d --build frontend      # only the UI (static/) changed
docker compose up -d --build               # several services changed at once
                                            # (unchanged services are skipped via
                                            # the build cache)
```

`db` almost never needs a rebuild — it uses the official upstream image
unchanged. When a change alters the database schema (a new table/column),
rerun `migrate` alongside the affected services so the new schema is in
place before they start:

```bash
docker compose up -d --build migrate api collector
```

## Rolling back

- **A single service misbehaves after a redeploy**: `git checkout <previous
  commit or tag> -- <path>` for the affected code, then rebuild just that
  service (e.g. `docker compose up -d --build api`). Because containers are
  independent, this doesn't require touching the others.
- **The whole stack needs to roll back**: `git checkout <previous commit>`,
  then `docker compose up -d --build`.
- **Falling back to the old venv/systemd deployment entirely**: follow
  `legacy/legacy-ubuntu-deploy.md`. Note that it expects
  `nginx-vcf-billing.conf` to point at `:8000`, while the v4.0 version of
  that file points at `:8080` — adjust the `proxy_pass` line back if you go
  this route.
- **Database rollback**: PostgreSQL data lives in the `db_data` Docker
  volume. Docker Compose does not back this up automatically — take your
  own `pg_dump` backups before any migration or schema change you're unsure
  about. `docker compose down -v` deletes the volume entirely (irreversible)
  and is normally only used to reset a test/demo environment from scratch.

## Troubleshooting

- **502 Bad Gateway from `frontend`**: check `docker compose logs api` to
  confirm the API actually started. If you just redeployed `api`, the
  internal nginx in `frontend` is already configured to re-resolve the
  `api` service name on every request via Docker's embedded DNS
  (`resolver 127.0.0.11` in `docker/frontend/nginx.conf`), so it normally
  follows automatically. If it still doesn't, `docker compose restart
  frontend`.
- **`migrate` fails and `api`/`collector` never start**: check `docker
  compose logs migrate`. This is almost always a mismatch between
  `DATABASE_URL` and `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB`
  in `.env`.
- **`db` is healthy but `migrate` keeps retrying**: `wait_for_db()`
  (`app/database.py`) retries for up to ~1 minute (30 attempts), so this
  usually resolves on its own. If it takes longer, check `docker compose
  logs db` for PostgreSQL's own startup log.
- **`docker compose build` hangs or fails while pulling images**: your
  network/firewall may be blocking Docker Hub (`registry-1.docker.io`). If
  you have an internal registry mirror, point the `FROM` line in each
  `docker/*/Dockerfile` at that mirror instead.
- **Integration sync fails with `[Errno -3] Temporary failure in name
  resolution`**: this is a DNS lookup failure for your VCF
  Operations hostname, happening *inside* the `api`/
  `collector` containers rather than on the host. It's almost always caused
  by Ubuntu's systemd-resolved: the host's `/etc/resolv.conf` points at the
  stub resolver `127.0.0.53`, which only works on the host itself — a
  container's separate network namespace can't reach it, so Docker's
  container DNS resolution silently breaks for internal/corporate-only
  hostnames (the host itself still resolves them fine, which is why this
  only shows up after moving to Docker). Confirm this by comparing:

  ```bash
  getent hosts <your-vcf-ops-hostname>                    # works on the host
  docker compose exec api getent hosts <your-vcf-ops-hostname>  # fails in the container
  ```

  Fix it by pointing the containers at the same DNS server(s) your host
  actually uses upstream. Find that IP with:

  ```bash
  resolvectl status | grep -A3 "Current DNS Server"
  ```

  then set it in `.env` (already wired into `docker-compose.yml` via the
  `dns:` key on the `api`/`collector`/`migrate` services):

  ```bash
  DOCKER_DNS_1=<your internal DNS server IP>
  DOCKER_DNS_2=8.8.8.8   # optional public fallback
  ```

  Apply it and re-test:

  ```bash
  docker compose up -d --force-recreate api collector
  docker compose exec api getent hosts <your-vcf-ops-hostname>
  ```

  Then retry "가져오기" (sync now) from the admin UI. If the hostname is
  fixed and you'd rather not expose your internal DNS server to the
  containers at all, `extra_hosts` is a narrower alternative — add a static
  hostname-to-IP mapping under the `api`/`collector` services instead of
  `dns:` (trades off needing an update if that IP ever changes):

  ```yaml
      extra_hosts:
        - "<your-vcf-ops-hostname>:<its IP address>"
  ```

## Where to go next

- README.md — feature reference, admin UI walkthrough, real VCF
  Operations integration guide, known limitations.
- `legacy/legacy-ubuntu-deploy.md` — the pre-v4.0 venv + systemd install
  guide, kept as a fallback for environments where Docker isn't an option.
