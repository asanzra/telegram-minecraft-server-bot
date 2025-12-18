# Release Notes

## v1.0.0 — Initial Public Release

Highlights
- Env-only configuration: `.env` via `python-dotenv`. No `config.py`.
- Docker Compose integration: server lifecycle via `docker compose up/down`.
- Readiness confirmation: start confirmation waits until status is joinable.
- Healthy confirmation broadcast: messages sent on `manual_start_confirmed` and monitor `health_ok`.
- Telegram bot hardening: role-based access, user/chat management, broadcast utility.
- Dev tooling: `ruff` linting and formatting; Make targets for install/lint.
- Documentation & hygiene: comprehensive `README.md`, CC BY-NC 4.0 license, runtime artifacts ignored.

Commands
- `/server_start` — start server and wait for healthy confirmation.
- `/server_stop` — stop server and record session.
- `/server_status` — status + health, shows online players when available.
- `/server_logs` — recent Compose logs.
- `/server_stats` — starts/stops counters and last events.
- `/server_uptime_log` — recent uptime events.
- `/server_historic` — aggregated historic uptime.
- Admin: `/add`, `/remove_user`, `/list_users`, `/add_chat`, `/remove_chat`, `/list_chats`, `/broadcast`, `/shutdown_bot`.

Technical Notes
- Uses `pyTelegramBotAPI` and `python-dotenv`.
- RCON via `docker compose exec <service> rcon-cli` (service auto-detected if not set).
- Monitor thread with grace window for transient unhealthy.
- Session and stats persisted to JSON; atomic writes in `access.py`.

Upgrade Guide
- Create `.env` from `.env.example` with `TELEGRAM_TOKEN`, `ADMIN_ID`, `COMPOSE_DIR`, optional `RCON_SERVICE`.
- Ensure Docker Compose v2 and a Minecraft server container (e.g., `itzg/docker-minecraft-server`).
- Authorize users/chats before use.

CI
- Added GitHub Actions workflow (`.github/workflows/ci.yml`) to run `make install` and `make lint` on pushes and PRs.

Known Limitations
- CI does not run the server or integration tests; it focuses on packaging and linting.
- Health detection depends on Compose `ps` reporting `State`/`Health` fields.
