import subprocess
import json
from pathlib import Path
import threading
import time
import datetime
from typing import Dict, List, Callable, Optional
import logging
import re

logger = logging.getLogger(__name__)


def escape_minecraft_command_returns(unescaped_text):
    ansi_escape = re.compile(
        r"""
        \x1B  # ESC
        (?:   # 7-bit C1 Fe (except CSI)
            [@-Z\\-_]
        |     # or [ for CSI, followed by a control sequence
            \[
            [0-?]*  # Parameter bytes
            [ -/]*  # Intermediate bytes
            [@-~]   # Final byte
        )
    """,
        re.VERBOSE,
    )
    result = ansi_escape.sub("", unescaped_text)
    return result


class MinecraftServerManager:
    """
    Manages a docker-compose based Minecraft server:
      - start/stop server
      - monitor status & health with smoothing/grace window
      - notify registered listeners about events
      - record uptime sessions and stats (saved to JSON files)
    """

    def __init__(
        self,
        compose_dir: str,
        monitor_interval: int = 60,
        start_timeout: int = 360,
        start_poll_interval: int = 5,
        health_grace_seconds: int = 120,
        rcon_service: Optional[str] = None,
    ):
        # Basic config
        self.compose_dir = Path(compose_dir)
        self.validate_setup()

        # Files
        self.log_file = Path("server_uptime.log")
        self.stats_file = Path("server_stats.json")
        self.sessions_file = Path("server_sessions.json")

        # Monitoring / timing config
        self.monitor_interval = monitor_interval
        self.start_timeout = start_timeout
        self.start_poll_interval = start_poll_interval
        self.health_grace_seconds = health_grace_seconds
        self.rcon_service = rcon_service

        # Concurrency primitives
        self._event_lock = threading.Lock()
        self._io_lock = threading.Lock()  # protect file IO
        self._start_lock = threading.Lock()  # serialize manual starts
        self._stop_event = threading.Event()  # wakeable stop for monitor thread

        # Event listeners and state
        self._event_listeners: List[Callable[[dict], None]] = []
        self._watcher_started = False

        # Monitoring state
        self.last_known_status: Optional[bool] = None
        self._last_known_health: Optional[str] = None
        self.current_session_start: Optional[datetime.datetime] = None

        # Manual start state
        self.start_pending: bool = False
        self.start_requested_at: Optional[datetime.datetime] = None

        # Thread handle
        self.monitor_thread: Optional[threading.Thread] = None

        # Start background monitor
        self.start_monitoring()

    # -------------------
    # Setup / registration
    # -------------------
    def validate_setup(self) -> None:
        """Ensure compose dir & docker-compose.yml exist."""
        if not self.compose_dir.exists():
            raise FileNotFoundError(
                f"Docker compose directory does not exist: {self.compose_dir}"
            )
        if not (self.compose_dir / "docker-compose.yml").exists():
            raise FileNotFoundError(
                "docker-compose.yml file not found in compose directory"
            )

    def register_event_listener(self, callback: Callable[[dict], None]) -> None:
        """Register a callable to receive events. Duplicate registrations are ignored."""
        if not callable(callback):
            return
        with self._event_lock:
            if callback not in self._event_listeners:
                self._event_listeners.append(callback)

    def unregister_event_listener(self, callback: Callable[[dict], None]) -> None:
        """Remove previously registered event listener (silently ignore if missing)."""
        with self._event_lock:
            if callback in self._event_listeners:
                self._event_listeners.remove(callback)

    def _dispatch_event(self, event: dict) -> None:
        """
        Safely emit an event to all listeners. Exceptions from listeners are swallowed
        to avoid crashing the manager, but logged for diagnostics.
        """
        with self._event_lock:
            listeners = list(self._event_listeners)

        for listener in listeners:
            try:
                listener(event)
            except Exception:
                logger.exception("Event listener raised an exception")

    # -------------------
    # Docker / container parsing
    # -------------------
    def _get_containers_info(self) -> List[Dict]:
        """
        Return list of dicts: {service, state, health}. Returns empty list on failure.
        Attempts to parse either a JSON array or newline-delimited JSON objects.
        """
        try:
            result = subprocess.run(
                ["docker", "compose", "ps", "--format", "json"],
                cwd=self.compose_dir,
                capture_output=True,
                text=True,
                check=True,
            )
            stdout = result.stdout.strip()
            if not stdout:
                return []

            # Try parsing as a JSON array first
            try:
                data = json.loads(stdout)
                # Expecting either a list of objects or an object - normalize to list
                if isinstance(data, dict):
                    data = [data]
                containers = []
                for container in data:
                    containers.append(
                        {
                            "service": container.get("Service", "unknown"),
                            "state": container.get("State", "").lower(),
                            "health": container.get("Health", "").lower(),
                        }
                    )
                return containers
            except json.JSONDecodeError:
                # Fallback: parse either newline-delimited JSON objects or each line separately
                containers = []
                for line in stdout.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        containers.append(
                            {
                                "service": obj.get("Service", "unknown"),
                                "state": obj.get("State", "").lower(),
                                "health": obj.get("Health", "").lower(),
                            }
                        )
                    except json.JSONDecodeError:
                        # ignore unparsable lines
                        continue
                return containers
        except subprocess.CalledProcessError as e:
            logger.exception("docker compose ps failed: %s", e)
            return []
        except Exception:
            logger.exception("Unexpected error when calling docker compose ps")
            return []

    # -------------------
    # File-safe helpers (IO-locked)
    # -------------------
    def _log_event(self, event: str, reason: str = "") -> None:
        """Append a timestamped event to the uptime log file (thread-safe)."""
        timestamp = datetime.datetime.now().isoformat()
        log_entry = f"{timestamp} - {event}"
        if reason:
            log_entry += f" - {reason}"
        try:
            with self._io_lock:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(log_entry + "\n")
        except Exception:
            logger.exception("Failed to write uptime log")

    def _load_stats(self) -> Dict:
        """Load statistics from file (returns defaults when missing or on error)."""
        try:
            with self._io_lock:
                if self.stats_file.exists():
                    with open(self.stats_file, "r", encoding="utf-8") as f:
                        return json.load(f)
        except Exception:
            logger.exception("Failed to load stats file")
        return {"total_starts": 0, "daily": {}, "last_start": None, "last_stop": None}

    def _save_stats(self, stats: Dict) -> None:
        """Save statistics to disk (atomicity not guaranteed, but IO locked)."""
        try:
            with self._io_lock:
                with open(self.stats_file, "w", encoding="utf-8") as f:
                    json.dump(stats, f, indent=2)
        except Exception:
            logger.exception("Failed to save stats file")

    def _load_sessions(self) -> List[Dict]:
        try:
            with self._io_lock:
                if self.sessions_file.exists():
                    with open(self.sessions_file, "r", encoding="utf-8") as f:
                        return json.load(f)
        except Exception:
            logger.exception("Failed to load sessions file")
        return []

    def _save_sessions(self, sessions: List[Dict]) -> None:
        try:
            with self._io_lock:
                with open(self.sessions_file, "w", encoding="utf-8") as f:
                    json.dump(sessions[-100:], f, indent=2)
        except Exception:
            logger.exception("Failed to save sessions file")

    def _log_session(
        self,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        start_reason: str,
        stop_reason: str,
    ) -> None:
        """Record a completed session (duration in hours, rounded to 2 decimals)."""
        try:
            duration_hours = (end_time - start_time).total_seconds() / 3600.0
            session = {
                "start": start_time.isoformat(),
                "end": end_time.isoformat(),
                "duration_hours": round(duration_hours, 2),
                "start_reason": start_reason,
                "stop_reason": stop_reason,
            }
            sessions = self._load_sessions()
            sessions.append(session)
            self._save_sessions(sessions)
        except Exception:
            logger.exception("Failed to log session")

    # -------------------
    # Monitor thread
    # -------------------
    def start_monitoring(self) -> None:
        """Start the background monitor thread (idempotent)."""
        with self._event_lock:
            if self._watcher_started:
                return
            self._watcher_started = True

        self._stop_event.clear()
        self.monitor_thread = threading.Thread(target=self._monitor_server, daemon=True)
        self.monitor_thread.start()

    def stop_monitoring_thread(self, timeout: float = 5.0) -> None:
        """Request the monitor thread to stop and join it (if running)."""
        self._stop_event.set()
        if self.monitor_thread and self.monitor_thread.is_alive():
            self.monitor_thread.join(timeout=timeout)

    def _monitor_server(self) -> None:
        """
        Background monitor that polls container states and emits events on changes.
        Uses _stop_event.wait(timeout) so it can be woken quickly.
        """
        unhealthy_since: Optional[datetime.datetime] = None
        prev_health: Optional[str] = None

        while not self._stop_event.is_set():
            try:
                containers = self._get_containers_info()
                any_running = any(c.get("state") == "running" for c in containers)
                health_states = [c.get("health") for c in containers if c.get("health")]
                state_states = [c.get("state") for c in containers if c.get("state")]

                if any(h and "unhealthy" in h for h in health_states):
                    consolidated_health = "unhealthy"
                elif any("starting" in (h or "") for h in health_states) or any(
                    "starting" in (s or "") for s in state_states
                ):
                    consolidated_health = "starting"
                elif any_running:
                    consolidated_health = "running"
                else:
                    consolidated_health = "stopped"

                current_status = any_running
                now = datetime.datetime.now()

                # Track unhealthy start time for grace
                if consolidated_health == "unhealthy":
                    if unhealthy_since is None:
                        unhealthy_since = now
                else:
                    unhealthy_since = None

                # Apply grace: treat 'unhealthy' as 'starting' while it's younger than grace_seconds
                effective_health = consolidated_health
                if consolidated_health == "unhealthy" and unhealthy_since:
                    age = (now - unhealthy_since).total_seconds()
                    if age < self.health_grace_seconds:
                        effective_health = "starting"

                # Detect running <-> stopped transitions
                if (
                    self.last_known_status is not None
                    and current_status != self.last_known_status
                ):
                    if current_status:
                        # transitioned to running
                        # Only auto-detect session start if not already set (avoid double-set)
                        if not self.current_session_start:
                            self.current_session_start = datetime.datetime.now()
                        self._log_event("SERVER_START", "auto_detected")
                        # Note: update_stats increments total_starts and daily counts
                        self._update_stats("start")
                        self._dispatch_event(
                            {
                                "type": "server_start",
                                "message": "🟠 Minecraft server is starting (auto-detected).",
                                "containers": containers,
                            }
                        )
                    else:
                        # transitioned to stopped
                        if self.current_session_start:
                            self._log_session(
                                self.current_session_start,
                                datetime.datetime.now(),
                                "auto_detected",
                                "auto_detected",
                            )
                            self.current_session_start = None
                        self._log_event("SERVER_STOP", "auto_detected")
                        self._update_stats("stop")
                        self._dispatch_event(
                            {
                                "type": "server_stop",
                                "message": "🟠 Minecraft server has stopped (auto-detected).",
                                "containers": containers,
                            }
                        )

                # Health transition notifications (respect grace window)
                if prev_health != effective_health:
                    if effective_health == "unhealthy":
                        # only emit if unhealthy persisted beyond grace window
                        if (
                            unhealthy_since is None
                            or (
                                datetime.datetime.now() - unhealthy_since
                            ).total_seconds()
                            >= self.health_grace_seconds
                        ):
                            self._log_event(
                                "SERVER_HEALTH_ISSUE", "unhealthy_persisted"
                            )
                            self._dispatch_event(
                                {
                                    "type": "health_unhealthy",
                                    "message": "🟡 Server health: UNHEALTHY (persisted).",
                                    "containers": containers,
                                }
                            )
                    elif effective_health == "running":
                        self._dispatch_event(
                            {
                                "type": "health_ok",
                                "message": "🟢 Server health: OK (running).",
                                "containers": containers,
                            }
                        )

                prev_health = effective_health
                self.last_known_status = current_status

            except Exception:
                logger.exception("Monitor loop error (continuing)")

            # Wait but allow immediate stop via _stop_event
            self._stop_event.wait(timeout=self.monitor_interval)

    # -------------------
    # Server control
    # -------------------
    def start_server(self) -> Dict:
        """
        Trigger 'docker compose up -d' to start the server.
        Returns immediately with status 'starting'. The start-watcher thread will
        confirm success/failure and emit events. Stats/sessions are updated only on confirmation.
        """
        with self._start_lock:
            if self.start_pending:
                return {"status": "pending", "message": "Start already pending"}

            # If server is already running, ignore duplicate manual start to preserve session
            try:
                containers = self._get_containers_info()
                already_running = any(c.get("state") == "running" for c in containers)
            except Exception:
                already_running = False

            if already_running or self.current_session_start is not None:
                return {
                    "status": "running",
                    "message": "🟢 Server already running; ignoring duplicate start.",
                }

            try:
                result = subprocess.run(
                    ["docker", "compose", "up", "-d"],
                    cwd=self.compose_dir,
                    check=True,
                    text=True,
                    capture_output=True,
                )
            except subprocess.CalledProcessError as e:
                logger.exception("Failed to run docker compose up")
                return {
                    "status": "error",
                    "message": "🔴 Failed to start server (docker compose error)",
                    "error": f"{e.stderr}\nExit code: {e.returncode}",
                }
            except Exception as e:
                logger.exception("Unexpected error while starting server")
                return {
                    "status": "error",
                    "message": "⚪ Unexpected error occurred",
                    "error": str(e),
                }

            # Mark pending start and spawn watcher that will confirm success/failure
            self.start_pending = True
            self.start_requested_at = datetime.datetime.now()

            # Dispatch preliminary event
            self._dispatch_event(
                {
                    "type": "server_start",
                    "message": "🟠 Minecraft server is starting (manual request).",
                }
            )

            def _is_server_ready() -> bool:
                """Use the same criteria as server_status: running and not unhealthy.
                This aligns confirmation with when players can join.
                """
                try:
                    st = self.server_status()
                    return st.get("status") == "running"
                except Exception:
                    return False

            def _start_watcher():
                deadline = datetime.datetime.now() + datetime.timedelta(
                    seconds=self.start_timeout
                )
                early_fail_window = 10
                early_fail_deadline = datetime.datetime.now() + datetime.timedelta(
                    seconds=early_fail_window
                )
                last_unhealthy_seen: Optional[datetime.datetime] = None

                try:
                    while self.start_pending and datetime.datetime.now() < deadline:
                        try:
                            containers = self._get_containers_info()
                            now = datetime.datetime.now()

                            any_running = any(
                                c.get("state") == "running" for c in containers
                            )
                            health_states = [
                                c.get("health") for c in containers if c.get("health")
                            ]
                            state_states = [
                                c.get("state") for c in containers if c.get("state")
                            ]

                            has_exited = any(
                                s and ("exited" in s or "dead" in s)
                                for s in state_states
                            )
                            no_containers = len(containers) == 0

                            if any(h and "unhealthy" in h for h in health_states):
                                if last_unhealthy_seen is None:
                                    last_unhealthy_seen = now
                            else:
                                last_unhealthy_seen = None

                            # Success: running AND readiness indicators satisfied
                            if any_running:
                                # Do not confirm while unhealthy/starting states present
                                if any(
                                    h and "unhealthy" in h for h in health_states
                                ) or any(s and "starting" in s for s in state_states):
                                    time.sleep(self.start_poll_interval)
                                    continue
                                # Enforce a short settle delay to avoid instant confirmation
                                try:
                                    since_request = (
                                        datetime.datetime.now()
                                        - (
                                            self.start_requested_at
                                            or datetime.datetime.now()
                                        )
                                    ).total_seconds()
                                except Exception:
                                    since_request = 0
                                if since_request < 5:
                                    time.sleep(self.start_poll_interval)
                                    continue
                                # Confirm only when status reports running/healthy
                                if not _is_server_ready():
                                    time.sleep(self.start_poll_interval)
                                    continue
                                self.start_pending = False
                                # Only set a new session start if none is active; avoid corrupting an existing session
                                if self.current_session_start is None:
                                    self.current_session_start = datetime.datetime.now()
                                    self._log_event(
                                        "SERVER_START_CONFIRMED",
                                        "manual_start_confirmed",
                                    )
                                    self._update_stats("start")
                                    self._dispatch_event(
                                        {
                                            "type": "manual_start_confirmed",
                                            "message": "🟢 Minecraft server started successfully!",
                                            "containers": containers,
                                        }
                                    )
                                else:
                                    # Duplicate start while already running; do not alter session or stats
                                    self._log_event(
                                        "SERVER_START", "manual_start_ignored_duplicate"
                                    )
                                    self._dispatch_event(
                                        {
                                            "type": "manual_start_duplicate",
                                            "message": "🟢 Server already running; duplicate start ignored.",
                                            "containers": containers,
                                        }
                                    )
                                return

                            # Early failure if containers exited/crashed
                            if has_exited:
                                self.start_pending = False
                                self._log_event(
                                    "START_FAILED", "container_exited_during_start"
                                )
                                self._dispatch_event(
                                    {
                                        "type": "manual_start_failed",
                                        "message": "🔴 Minecraft server failed to start (container exited/crashed).",
                                        "containers": containers,
                                    }
                                )
                                return

                            # No containers after early fail window -> failure
                            if (
                                no_containers
                                and datetime.datetime.now() >= early_fail_deadline
                            ):
                                self.start_pending = False
                                self._log_event(
                                    "START_FAILED", "no_containers_after_start"
                                )
                                self._dispatch_event(
                                    {
                                        "type": "manual_start_failed",
                                        "message": f"🔴 Minecraft server did not start (no containers present after {early_fail_window}s).",
                                        "containers": containers,
                                    }
                                )
                                return

                        except Exception:
                            logger.exception(
                                "start_watcher poll exception (will retry)"
                            )

                        # Sleep a bit between polls but allow thread to be cooperative
                        time.sleep(self.start_poll_interval)

                    # If still pending after deadline -> timeout
                    if self.start_pending:
                        self.start_pending = False
                        self._log_event("START_FAILED", "manual_start_timeout")
                        self._dispatch_event(
                            {
                                "type": "manual_start_failed",
                                "message": f"🔴 Minecraft server did not become healthy within {self.start_timeout} seconds.",
                                "containers": self._get_containers_info(),
                            }
                        )
                except Exception:
                    logger.exception("Unhandled exception in start watcher")
                    self.start_pending = False

            # spawn watcher
            watcher_thread = threading.Thread(target=_start_watcher, daemon=True)
            watcher_thread.start()

            return {
                "status": "starting",
                "message": "🟠 Minecraft server is starting (manual request).",
                "details": result.stdout,
            }

    def stop_server(self) -> Dict:
        """Stop the Minecraft server (docker compose down)."""
        try:
            result = subprocess.run(
                ["docker", "compose", "down"],
                cwd=self.compose_dir,
                check=True,
                text=True,
                capture_output=True,
            )
            # Emit event and log
            self._dispatch_event(
                {
                    "type": "server_stop",
                    "message": "🟠 Minecraft server has stopped (manual request).",
                }
            )

            # If we had an active session, record it
            if self.current_session_start:
                try:
                    self._log_session(
                        self.current_session_start,
                        datetime.datetime.now(),
                        "manual_start",
                        "manual_stop",
                    )
                except Exception:
                    logger.exception("Failed to log manual stop session")
                finally:
                    self.current_session_start = None

            self._log_event("SERVER_STOP", "manual_stop")
            self._update_stats("stop")

            return {
                "status": "stopped",
                "message": "🟠 Minecraft server stopped successfully!",
                "details": result.stdout,
            }
        except subprocess.CalledProcessError as e:
            logger.exception("docker compose down failed")
            return {
                "status": "error",
                "message": "🔴 Failed to stop server",
                "error": f"{e.stderr}\nExit code: {e.returncode}",
            }
        except Exception:
            logger.exception("Unexpected error while stopping server")
            return {
                "status": "error",
                "message": "⚪ Unexpected error occurred during stop",
            }

    def add_whitelist(self, username) -> str:
        """Add someone to the server whitelist (via docker compose exec on the configured or detected service)."""
        try:
            service = self.rcon_service
            if not service:
                # try to detect a service from compose ps
                containers = self._get_containers_info()
                if containers:
                    service = containers[0].get("service") or "mc"
                else:
                    service = "mc"
            result = subprocess.run(
                [
                    "docker",
                    "compose",
                    "exec",
                    "-T",
                    service,
                    "rcon-cli",
                    f"whitelist add {username}",
                ],
                cwd=self.compose_dir,
                check=True,
                text=True,
                capture_output=True,
            ).stdout
            result = escape_minecraft_command_returns(result)
            return {"status": "success", "message": result}
        except subprocess.CalledProcessError as e:
            return {
                "status": "error",
                "message": f"Failed to add to whitelist on service '{service}': {e.stderr}",
            }
        except Exception as e:
            return {
                "status": "error",
                "message": f"Unexpected error adding to whitelist: {e}",
            }

    # -------------------
    # Status & logs
    # -------------------
    def server_status(self) -> Dict:
        """Return parsed status for containers, plus an overall message."""
        try:
            result = subprocess.run(
                ["docker", "compose", "ps", "--format", "json"],
                cwd=self.compose_dir,
                check=True,
                text=True,
                capture_output=True,
            )
            containers = []
            status = "stopped"
            message = "🔴 Server is stopped"

            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    container = json.loads(line)
                except json.JSONDecodeError:
                    # ignore unparseable lines
                    continue

                state = container.get("State", "").lower()
                health = container.get("Health", "").lower()
                service = container.get("Service", "unknown")
                containers.append(
                    {"service": service, "state": state, "health": health}
                )

                if "starting" in health or "starting" in state:
                    status = "starting"
                    message = "🟠 Server is starting..."
                elif "running" in state:
                    if status != "starting":
                        status = "running"
                        message = "🟢 Server is running!"
                        # Try to query online players via rcon-cli
                        service = self.rcon_service
                        if not service:
                            try:
                                containers = self._get_containers_info()
                                if containers:
                                    service = containers[0].get("service") or "mc"
                                else:
                                    service = "mc"
                            except Exception:
                                service = "mc"
                        try:
                            online_players = subprocess.run(
                                [
                                    "docker",
                                    "compose",
                                    "exec",
                                    "-T",
                                    service,
                                    "rcon-cli",
                                    "list",
                                ],
                                cwd=self.compose_dir,
                                check=True,
                                text=True,
                                capture_output=True,
                            ).stdout
                        except Exception:
                            online_players = ""
                        online_players = escape_minecraft_command_returns(
                            online_players
                        )
                        message += "\n" + online_players
                    if "unhealthy" in health:
                        status = "unhealthy"
                        message = "🟡 Server health: UNHEALTHY (may be initializing)."

            return {
                "status": status,
                "message": message,
                "containers": containers,
                "raw_output": result.stdout,
            }
        except subprocess.CalledProcessError as e:
            logger.exception("Failed to query server status")
            return {
                "status": "error",
                "message": "🔴 Error checking status",
                "error": e.stderr,
            }
        except Exception:
            logger.exception("Unexpected error while checking status")
            return {
                "status": "error",
                "message": "⚪ Unexpected error occurred while checking status",
            }

    def get_logs(self, lines: int = 20) -> Dict:
        """Return last N lines of docker compose logs."""
        try:
            result = subprocess.run(
                ["docker", "compose", "logs", "--tail", str(lines)],
                cwd=self.compose_dir,
                check=True,
                text=True,
                capture_output=True,
            )
            return {
                "status": "success",
                "message": f"📜 Last {lines} lines of logs:",
                "logs": result.stdout,
            }
        except subprocess.CalledProcessError as e:
            logger.exception("Failed to fetch logs")
            return {
                "status": "error",
                "message": "🔴 Failed to get logs",
                "error": e.stderr,
            }
        except Exception:
            logger.exception("Unexpected error getting logs")
            return {
                "status": "error",
                "message": "⚪ Unexpected error occurred while fetching logs",
            }

    # -------------------
    # Uptime/stats helpers (public)
    # -------------------
    def _update_stats(self, action: str) -> None:
        """Update daily/total stats. action in {'start', 'stop'}."""
        try:
            stats = self._load_stats()
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            if "daily" not in stats:
                stats["daily"] = {}
            if today not in stats["daily"]:
                stats["daily"][today] = 0

            if action == "start":
                stats["daily"][today] += 1
                stats["total_starts"] = stats.get("total_starts", 0) + 1
                stats["last_start"] = datetime.datetime.now().isoformat()
            elif action == "stop":
                stats["last_stop"] = datetime.datetime.now().isoformat()

            self._save_stats(stats)
        except Exception:
            logger.exception("Failed to update stats")

    def get_uptime_stats(self) -> Dict:
        """Return aggregated uptime stats and last 7 days starts count."""
        try:
            stats = self._load_stats()

            manual_starts = manual_stops = auto_starts = auto_stops = 0
            if self.log_file.exists():
                with self._io_lock:
                    with open(self.log_file, "r", encoding="utf-8") as f:
                        lines = f.readlines()
                for line in lines:
                    if "SERVER_START" in line:
                        if "manual_start" in line:
                            manual_starts += 1
                        elif "auto_detected" in line:
                            auto_starts += 1
                    elif "SERVER_STOP" in line:
                        if "manual_stop" in line:
                            manual_stops += 1
                        elif "auto_detected" in line:
                            auto_stops += 1

            # build 7-day series
            daily_stats = []
            today = datetime.datetime.now()
            for i in range(7):
                date = (today - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
                count = stats.get("daily", {}).get(date, 0)
                daily_stats.append({"date": date, "starts": count})

            return {
                "status": "success",
                "message": "📊 Server Uptime Statistics",
                "stats": {
                    "total_starts": stats.get("total_starts", 0),
                    "manual_starts": manual_starts,
                    "auto_starts": auto_starts,
                    "manual_stops": manual_stops,
                    "auto_stops": auto_stops,
                    "last_start": stats.get("last_start"),
                    "last_stop": stats.get("last_stop"),
                    "daily_stats": daily_stats,
                },
            }
        except Exception:
            logger.exception("Failed to build uptime stats")
            return {"status": "error", "message": "🔴 Failed to get uptime statistics"}

    def get_uptime_log(self, lines: int = 10) -> Dict:
        """Return the last N lines from the uptime log."""
        try:
            if not self.log_file.exists():
                return {
                    "status": "success",
                    "message": "No uptime log entries yet",
                    "logs": [],
                }
            with self._io_lock:
                with open(self.log_file, "r", encoding="utf-8") as f:
                    all_lines = f.readlines()
            recent_lines = all_lines[-lines:] if lines > 0 else all_lines
            return {
                "status": "success",
                "message": f"📋 Last {len(recent_lines)} uptime events:",
                "logs": [line.strip() for line in recent_lines],
            }
        except Exception:
            logger.exception("Failed to read uptime log")
            return {"status": "error", "message": "🔴 Failed to get uptime log"}

    def get_historic_uptime(self) -> Dict:
        """Compute historic uptime statistics from recorded sessions."""
        try:
            sessions = self._load_sessions()
            if not sessions:
                return {
                    "status": "success",
                    "message": "No historic session data available yet",
                    "data": {
                        "total_uptime_hours": 0,
                        "total_sessions": 0,
                        "average_session_hours": 0,
                        "longest_session_hours": 0,
                        "uptime_by_day": {},
                    },
                }

            total_uptime_hours = sum(
                session.get("duration_hours", 0) for session in sessions
            )
            total_sessions = len(sessions)
            average_session_hours = (
                total_uptime_hours / total_sessions if total_sessions else 0
            )
            longest_session_hours = max(
                (session.get("duration_hours", 0) for session in sessions), default=0
            )

            uptime_by_day: Dict[str, float] = {}
            for session in sessions:
                start_date = datetime.datetime.fromisoformat(session["start"]).strftime(
                    "%Y-%m-%d"
                )
                uptime_by_day.setdefault(start_date, 0.0)
                uptime_by_day[start_date] += session.get("duration_hours", 0.0)

            return {
                "status": "success",
                "message": "Historic uptime statistics",
                "data": {
                    "total_uptime_hours": round(total_uptime_hours, 2),
                    "total_sessions": total_sessions,
                    "average_session_hours": round(average_session_hours, 2),
                    "longest_session_hours": round(longest_session_hours, 2),
                    "uptime_by_day": uptime_by_day,
                },
            }
        except Exception:
            logger.exception("Failed to compute historic uptime")
            return {"status": "error", "message": "Failed to calculate historic uptime"}

    def get_monitoring_status(self) -> Dict:
        """Return monitoring thread & basic status info."""
        try:
            auto_events = 0
            if self.log_file.exists():
                with self._io_lock:
                    with open(self.log_file, "r", encoding="utf-8") as f:
                        for line in f:
                            if "auto_detected" in line:
                                auto_events += 1

            return {
                "status": "success",
                "message": "Monitoring status",
                "data": {
                    "monitor_running": bool(
                        self.monitor_thread and self.monitor_thread.is_alive()
                    ),
                    "check_interval_seconds": self.monitor_interval,
                    "last_known_status": "running"
                    if self.last_known_status
                    else "stopped",
                    "auto_detected_events": auto_events,
                    "current_session_active": self.current_session_start is not None,
                },
            }
        except Exception:
            logger.exception("Failed to build monitoring status")
            return {"status": "error", "message": "Failed to get monitoring status"}

    # -------------------
    # Shutdown / cleanup
    # -------------------
    def close(self) -> None:
        """Gracefully stop background threads. Call this at application shutdown."""
        try:
            self.stop_monitoring_thread()
        except Exception:
            logger.exception("Error while closing manager")

    # Keep __del__ out — explicit close() is more reliable
