"""An opt-in, single-flight local scan controller; no shell or account API."""

from __future__ import annotations

import asyncio
import copy
import logging
import secrets
import threading
import time
import uuid
from pathlib import Path

from config.loader import ScanConfig

logger = logging.getLogger("mtselector.dashboard")
MODES = ("mtproto", "telegram", "web", "webproxy")
MAX_BODY = 4096


class ControlError(ValueError):
    """A safe, user-facing validation or job conflict."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class ScanController:
    """Own at most one worker and pass only a copied configuration to execute.

    execute(config, mode, limit, command, resume, stop_event) performs the scan.
    It must honor stop_event and return only when measurements/export have stopped.
    A literal False return or cooperative cancellation means stopped, not complete.
    A stop request never claims the worker has already finished.
    """

    def __init__(self, config, execute):
        if not callable(execute):
            raise TypeError("A scan callback is required")
        self._config = copy.deepcopy(config)
        self._execute = execute
        self.token = secrets.token_urlsafe(32)
        self._lock = threading.Lock()
        self._worker = None
        self._stop = threading.Event()
        self._closed = False
        self._job = None

    def status(self):
        with self._lock:
            return {
                "enabled": not self._closed,
                "network": self._config.scan.network,
                "job": dict(self._job) if self._job else None,
            }

    def request(self, body):
        if not isinstance(body, dict) or set(body) - {
            "action",
            "mode",
            "network",
            "count",
            "minutes",
            "command",
        }:
            raise ControlError("Unknown scan options")
        action = body.get("action")
        if action not in ("start", "stop", "resume", "recheck", "favourites"):
            raise ControlError("Choose start, stop, resume or recheck")
        mode = body.get("mode", "mtproto")
        if mode not in MODES:
            raise ControlError("Unknown proxy mode")
        network = body.get("network", self._config.scan.network)
        try:
            network = ScanConfig(network=network).validate().network
        except (TypeError, ValueError) as exc:
            raise ControlError("Network must be 1–32 letters, digits, underscores or hyphens") from exc
        limit = body.get("count")
        if limit == "ALL":
            limit = None
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 1_000_000):
            raise ControlError("Count must be ALL or a whole number from 1 to 1000000")
        minutes = body.get("minutes")
        if minutes is not None and (type(minutes) is not int or minutes not in (3, 10, 20)):
            raise ControlError("Choose a 3, 10 or 20 minute scan")
        if action in ("stop", "resume", "recheck", "favourites") and (
            minutes is not None or limit is not None
        ):
            raise ControlError("Count and time are available for a new scan only")
        if "command" in body and (action != "resume" or body["command"] not in ("best10", "recheck")):
            raise ControlError("Resume target must be a scan or a recheck")
        with self._lock:
            if self._closed:
                raise ControlError("Dashboard controls have stopped", 409)
            active = self._worker is not None and self._worker.is_alive()
            if action == "stop":
                if not active:
                    raise ControlError("There is no active dashboard scan", 409)
                if mode != self._job["mode"] or network != self._job["network"]:
                    raise ControlError("Select the active scan mode and network before stopping", 409)
                self._stop.set()
                self._job.update(state="stopping", message="Stopping safely and saving completed checks…")
                return dict(self._job)
            if active:
                raise ControlError("A scan is already running; stop it before starting another", 409)
            from utils.user_sources import apply_user_sources

            config = apply_user_sources(copy.deepcopy(self._config))
            config.scan.network = network
            config.scan.resume = action == "resume"
            if action == "favourites":
                from utils.favourites import FavouriteStore

                config._favourite_proxies = FavouriteStore(config).proxies(mode, network)
                if not config._favourite_proxies:
                    raise ControlError("No favourites saved for this mode and network")
                config.scan.time_budget_seconds = None
            if action == "start":
                # ALL/custom count are explicitly untimed, even when the
                # dashboard was launched with a CLI/config time budget.
                config.scan.time_budget_seconds = minutes * 60 if minutes is not None else None
            command = "recheck" if action == "recheck" else "best10"
            if action == "resume":
                command = body.get("command", "best10")
            self._stop = threading.Event()
            self._job = {
                "id": uuid.uuid4().hex,
                "state": "running",
                "action": action,
                "mode": mode,
                "network": network,
                "count": limit,
                "minutes": minutes,
                "command": command,
                "started_at": time.time(),
                "message": "Scan started; completed checks will appear below.",
            }
            self._worker = threading.Thread(
                target=self._run,
                args=(config, mode, limit, command, action == "resume", self._stop),
                name="revmamad-dashboard-scan",
                daemon=True,
            )
            try:
                self._worker.start()
            except (RuntimeError, OSError) as exc:
                self._job.update(
                    state="failed",
                    finished_at=time.time(),
                    message="Could not start a scan worker. Retry after closing another scan.",
                )
                self._worker = None
                raise ControlError("Could not start a scan worker", 503) from exc
            return dict(self._job)

    def library(self, body):
        """Authenticated local actions. No filenames, shell commands or account fields."""
        from dashboard import data
        from utils.favourites import FavouriteStore
        from utils.process_lock import OutputRootBusy
        from utils.scan_state import deserialize_proxy
        from utils.user_sources import SourceStore, canonical_mode

        schemas = {
            "list": {"action", "mode", "network"},
            "source_export": {"action"},
            "source_import": {"action", "document"},
            "source_add": {"action", "mode", "name", "url", "type", "protocol"},
            "source_manual": {"action", "mode", "name", "text"},
            "source_toggle": {"action", "id", "enabled"},
            "favourite_add": {"action", "mode", "network", "rank"},
            "favourite_remove": {"action", "id"},
            "favourite_recheck": {"action", "mode", "network"},
            "doctor": {"action"},
            "media_report": {"action", "mode", "network"},
        }
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("action"), str)
            or body["action"] not in schemas
            or set(body) - schemas[body["action"]]
        ):
            raise ControlError("Unknown personal-library options")
        action = body["action"]
        try:
            mode = body.get("mode", "mtproto")
            canonical_mode(mode)
            network = ScanConfig(network=body.get("network", self._config.scan.network)).validate().network
            sources = SourceStore(self._config)
            favourites = FavouriteStore(self._config)
            if action == "favourite_recheck":
                return {"job": self.request({"action": "favourites", "mode": mode, "network": network})}
            with self._lock:
                if self._closed:
                    raise ControlError("Dashboard controls have stopped", 409)
                if (
                    action not in {"list", "source_export", "doctor", "media_report"}
                    and self._worker is not None
                    and self._worker.is_alive()
                ):
                    raise ControlError("Stop the active scan before changing personal data", 409)
                if action == "list":
                    return {"sources": sources.list(mode), "favourites": favourites.list(mode, network)}
                if action == "source_export":
                    return {"document": sources.export_document()}
                if action == "doctor":
                    from utils.doctor import collect_diagnostics

                    return {"document": collect_diagnostics(self._config)}
                if action == "media_report":
                    from media_test.report import read_report
                    from scan import mode_folder

                    prepared = copy.copy(self._config)
                    prepared.scan = copy.copy(self._config.scan)
                    prepared.scan.network = network
                    report = read_report(mode_folder(prepared, mode) / "media-report.json")
                    if report is not None and report.get("network_profile") != network:
                        report = None
                    if report is not None:
                        report["results"] = [
                            row for row in report["results"] if row["network_profile"] == network
                        ]
                        identities = {row["candidate_id"] for row in report["results"]}
                        report["ranked_candidate_ids"] = [
                            identity for identity in report["ranked_candidate_ids"] if identity in identities
                        ]
                    return {"report": report}
                if action == "source_import":
                    return {"imported": sources.import_document(body.get("document"))}
                if action == "source_add":
                    return {
                        "added": sources.add_feed(
                            mode,
                            body.get("name"),
                            body.get("url"),
                            body.get("type", "http_txt"),
                            body.get("protocol", "http"),
                        )
                    }
                if action == "source_manual":
                    return {
                        "added": sources.import_manual(
                            mode, body.get("text"), body.get("name", "Personal links")
                        )
                    }
                if action == "source_toggle":
                    sources.set_enabled(body.get("id"), body.get("enabled"))
                    return {"saved": True}
                if action == "favourite_remove":
                    return {"removed": favourites.remove(body.get("id"))}
                rank = body.get("rank")
                if type(rank) is not int or not 1 <= rank <= 10:
                    raise ControlError("Choose one of the current top ten results")
                current = data.api_response(
                    "/api/snapshot", Path(self._config.output.folder), {"mode": [mode], "network": [network]}
                )
                row = next((row for row in current["rows"] if row["rank"] == rank), None)
                if row is None or row.get("evidence_status") != "fresh":
                    raise ControlError("Recheck this candidate before saving a favourite")
                kind = "mtproto" if mode in {"mtproto", "telegram"} else mode
                proxy = deserialize_proxy(
                    {**row, "kind": kind, "protocol": "mtproto" if kind == "mtproto" else row.get("protocol")}
                )
                return {"id": favourites.add(proxy, mode, network)}
        except OutputRootBusy as exc:
            raise ControlError(str(exc), 409) from exc
        except (ValueError, TypeError, OSError) as exc:
            if isinstance(exc, ControlError):
                raise
            raise ControlError(
                str(exc) if isinstance(exc, ValueError) else "Personal data could not be accessed"
            ) from exc

    def _run(self, config, mode, limit, command, resume, stop_event):
        failed = False
        try:
            completed = self._execute(config, mode, limit, command, resume, stop_event)
            if completed is False:
                stop_event.set()
        except (KeyboardInterrupt, asyncio.CancelledError):
            stop_event.set()
        except BaseException as exc:
            # SystemExit and unexpected BaseException subclasses must not leave
            # a phantom running job or expose an exit code/exception contents.
            logger.warning("Dashboard scan failed (%s)", type(exc).__name__)
            failed = True
        finally:
            with self._lock:
                stopped = stop_event.is_set()
                self._job.update(
                    state="failed" if failed else "stopped" if stopped else "complete",
                    finished_at=time.time(),
                    message=(
                        "Scan failed. Check the terminal, then retry or resume saved checks."
                        if failed
                        else (
                            "Stopped. Refresh saved results; resume is available when a checkpoint was saved."
                            if stopped
                            else "Scan finished. Refresh results below."
                        )
                    ),
                )

    def close(self, timeout=2):
        with self._lock:
            self._closed = True
            self._stop.set()
            worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=timeout)
