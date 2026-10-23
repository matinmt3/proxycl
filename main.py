"""REVMAMAD: independently verify and rank MTProto and web proxies."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import shutil
import sys
import textwrap
from collections import Counter
from dataclasses import replace
from pathlib import Path

from config.loader import AppConfig
from utils.logger import setup_logger

__version__ = "3.3.0"
PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


class PipelineResults(list):
    """List-compatible results retaining stop state for the scheduler."""

    def __init__(self, outcome):
        super().__init__(outcome.results)
        self.interrupted = outcome.summary.get("interrupted", False)


def load_config(path: str | Path | None = None) -> AppConfig:
    return AppConfig.load(path if path is not None else CONFIG_PATH)


def _width() -> int:
    return max(20, min(80, shutil.get_terminal_size(fallback=(40, 24)).columns))


def _message(message: str):
    print(textwrap.fill(message, width=_width(), break_long_words=False, break_on_hyphens=False))


def print_banner():
    """Compact ASCII art fits a 32-column phone terminal and needs no fonts/packages."""
    glyphs = {
        "R": ("## ", "# #", "## ", "# #", "# #"),
        "E": ("###", "#  ", "## ", "#  ", "###"),
        "V": ("# #", "# #", "# #", "# #", " # "),
        "M": ("# #", "###", "###", "# #", "# #"),
        "A": (" # ", "# #", "###", "# #", "# #"),
        "D": ("## ", "# #", "# #", "# #", "## "),
    }
    width = _width()
    art = [" ".join(glyphs[letter][row] for letter in "REVMAMAD").rstrip() for row in range(5)]
    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
    start, end = ("\033[96m", "\033[0m") if color else ("", "")
    print()
    print(start + "=" * min(width, 40))
    if width >= 31:
        print("\n".join(art))
    print(f"REVMAMAD v{__version__}")
    print("Telegram Links + HTTP / SOCKS")
    print("=" * min(width, 40) + end)


def _run_counts(proxies, results):
    verified = sum(r.success_rate > 0 and r.avg_latency_ms is not None for r in results)
    unsupported = sum(
        any(s.failure_reason.value == "unsupported_transport" for s in r.samples) for r in results
    )
    print()
    _message(
        f"Collected: {len(proxies)} | Tested: {len(results)} | Verified: {verified} | Unsupported: {unsupported}"
    )
    failures = Counter(s.failure_reason.value for r in results for s in r.samples if not s.success)
    if failures:
        _message(
            "Failed attempts: " + ", ".join(f"{reason}={count}" for reason, count in sorted(failures.items()))
        )


def run_pipeline(config: AppConfig, export: bool = True, *, mode: str = "mtproto", limit: int | None = None):
    # Keep HTTP/crypto imports lazy so --help, --version, and the dashboard work
    # independently of the scanner's optional installation state.
    from scan import run_scan

    outcome = run_scan(config, mode, limit, export)
    _message(f"Network: {config.scan.network} | Strategy: {outcome.summary.get('strategy', 'single')}")
    if outcome.summary.get("resumed"):
        _message("Resumed matching saved attempts; completed samples were not counted twice.")
    if outcome.summary.get("provisional"):
        _message(
            "PROVISIONAL: sustained finalist testing did not finish. These are strict observations so far."
        )
    if outcome.summary.get("strategy") == "staged":
        _message(
            f"Screened: {outcome.summary['prefilter_tested']} | Screen passed: {outcome.summary['prefilter_passed']} | Baseline finished: {outcome.summary['baseline_tested']}"
        )
        _message(
            f"Sustained finalists: {outcome.summary['finalist_completed']} / {outcome.summary['finalist_selected']} | Extra connections: {outcome.summary['finalist_attempts']}"
        )
        rejected = outcome.summary.get("prefilter_failure_counts", {})
        if rejected:
            _message(
                "Screening failures (not strict session attempts): "
                + ", ".join(f"{name}={count}" for name, count in sorted(rejected.items()))
            )
    if outcome.summary.get("interrupted"):
        _message(
            f"Stopped scan: {outcome.summary['tested']} / {outcome.summary['selected']} candidates have samples; "
            f"{outcome.summary['skipped']} untested. Showing the best verified results so far."
        )
        if outcome.summary["partial_tested"]:
            _message(
                "Interrupted retries retain finished samples. Success percentages use only finished attempts."
            )
    _message(
        f"Tested: {outcome.summary['tested']} | Verified: {outcome.summary['verified']} | Eligible: {outcome.summary['eligible']}"
    )
    if mode != "web":
        _message(
            f"Candidates with passing probes: {outcome.summary['transport_reachable']} | "
            f"Qualified Telegram sessions: {outcome.summary['strict_verified']}"
        )
        _message("Strict results need repeated authenticated sessions across the configured Telegram DCs.")
        if export:
            _message(f"All completed observations: {outcome.folder / 'observations.json'}")
    failures = outcome.summary["failure_counts"]
    if failures:
        _message("Failed attempts: " + ", ".join(f"{name}={count}" for name, count in failures.items()))
    failed_sources = sum(row.get("status") == "error" for row in outcome.source_reports)
    if failed_sources:
        _message(f"Unavailable sources: {failed_sources}. Other feeds continued.")
    return PipelineResults(outcome)


def print_summary(results, n: int = 10):
    print("\nRANK / SERVER / SCORE / SUCCESS / LATENCY")
    print("-" * min(_width(), 48))
    for i, result in enumerate(results[:n], 1):
        avg = f"{result.avg_latency_ms:.0f} ms" if result.avg_latency_ms is not None else "-"
        print(f"#{i} {result.proxy.server}:{result.proxy.port}")
        _message(f"  Score: {result.score:.3f} | Success: {result.success_rate:.0%} | Latency: {avg}")
    print()


def print_best10(results, output_folder: str, *, saved: bool = True, mode: str = "mtproto"):
    """Show up to ten verified proxies; a real scan may return fewer than ten."""
    healthy = [
        r
        for r in results
        if r.success_rate > 0 and r.avg_latency_ms is not None and (mode == "web" or r.strict_eligible)
    ]
    from benchmark.scorer import rank

    top = rank(healthy)[:10]
    print("\n" + "=" * min(_width(), 40))
    print(f"VERIFIED PROXIES: {len(top)} / 10")
    print("=" * min(_width(), 40))
    if len(top) < 10:
        _message(f"Showing {len(top)} verified proxies. Ten is the maximum, not a guaranteed count.")
        _message(
            "Network failures and your export filters can reduce this number. Check the failure counts above and try another source or network."
        )
    for i, result in enumerate(top, 1):
        print(
            f"\n#{i} {getattr(result.proxy, 'protocol', 'MTProto')} {result.proxy.server}:{result.proxy.port}"
        )
        if mode == "web":
            _message(f"Probe latency: {result.avg_latency_ms:.0f} ms")
        else:
            _message(
                f"Telegram RTT p95: {result.telegram_p95_ms:.0f} ms | "
                f"Median: {result.telegram_median_ms:.0f} ms | Jitter: {result.telegram_jitter_ms:.0f} ms"
            )
            _message(
                f"Authenticated reconnects: {result.strict_successes}/{result.attempts} | "
                f"Matched pongs: {result.ping_count} | DCs: {result.dc_ids_verified}"
            )
            _message(
                f"Reliability lower bound: {result.reliability_lower_bound:.1%} (95% Wilson). A short sample does not establish uptime."
            )
        _message(
            f"Pass rate: {result.success_rate:.0%} | Score: {result.score:.1%} | Samples: {result.attempts}"
        )
        if mode == "telegram":
            print(result.proxy.web_link())
        else:
            print(result.proxy.tg_link() if hasattr(result.proxy, "tg_link") else result.proxy.uri())
    print()
    if saved:
        _message(f"Saved to: {Path(output_folder) / 'best10_links.txt'}")


def cmd_collect(config: AppConfig, *, mode: str = "mtproto", limit: int | None = None):
    from scan import select_candidates, validate_request

    validate_request(mode, limit)
    if mode in ("mtproto", "telegram"):
        from collector.collector import Collector

        collector = Collector(config)
    else:
        from webproxy.collector import WebCollector

        collector = WebCollector(config)
    print(
        f"[{mode.upper()}] Collected {len(select_candidates(collector.collect(), limit))} unique proxy candidates (not yet verified)."
    )


def cmd_benchmark(config: AppConfig, *, mode: str = "mtproto", limit: int | None = None):
    from exporter.exporter import eligible_results
    from scan import mode_folder

    results = eligible_results(run_pipeline(config, export=False, mode=mode, limit=limit), config.output)
    print_best10(results, str(mode_folder(config, mode)), saved=False, mode=mode)


def cmd_export(config: AppConfig, *, mode: str = "mtproto", limit: int | None = None):
    cmd_best10(config, mode=mode, limit=limit)


def cmd_recheck(config: AppConfig, *, mode: str = "mtproto", limit: int | None = None):
    """Freshly probe up to twenty saved qualified endpoints on this network."""
    from config.loader import SourceConfig, WebSourceConfig
    from dashboard.data import _strict_qualified
    from exporter.exporter import Exporter
    from scan import mode_folder
    from utils.scan_state import deserialize_proxy

    folder = mode_folder(config, mode)
    candidate_file = folder / "recheck-candidates.txt"
    if not config.scan.resume:
        path = folder / "proxy.json"
        if not path.exists():
            raise ValueError("No saved candidates for this network/mode. Run best10 first.")
        if path.stat().st_size > 4 * 1024 * 1024 and (folder / "top10.json").is_file():
            path = folder / "top10.json"
        with path.open("rb") as stream:
            content = stream.read(4 * 1024 * 1024 + 1)
        if len(content) > 4 * 1024 * 1024:
            raise ValueError("Saved candidate list exceeds the recheck safety limit")
        rows = json.loads(content)
        if not isinstance(rows, list):
            raise ValueError("Invalid saved candidate list. Run a fresh best10 scan.")
        candidates = []
        for row in rows:
            if not isinstance(row, dict) or (mode != "web" and not _strict_qualified(row)):
                continue
            candidate = deserialize_proxy(
                {
                    "kind": "web" if mode == "web" else "mtproto",
                    **{
                        key: row[key]
                        for key in ("server", "port", "protocol" if mode == "web" else "secret")
                        if key in row
                    },
                    "source": "saved-top",
                }
            )
            candidates.append(candidate.uri() if mode == "web" else candidate.tg_link())
            if len(candidates) == 20:
                break
        if not candidates:
            raise ValueError("No qualified saved candidates. Run a fresh best10 scan.")
        Exporter(replace(config.output, folder=str(folder)))._write(
            candidate_file.name, "\n".join(candidates) + "\n"
        )
    elif not candidate_file.is_file():
        raise ValueError("No saved recheck candidate file. Start a fresh recheck.")
    prepared = replace(config, scan=replace(config.scan, source_health_enabled=False))
    source_type = WebSourceConfig if mode == "web" else SourceConfig
    selected_sources = [source_type("saved-top", "http_text", "file://" + str(candidate_file))]
    if mode == "web":
        prepared.web_sources = selected_sources
    else:
        prepared.sources = selected_sources
    prepared._scan_command = "recheck"
    return cmd_best10(prepared, mode=mode, limit=limit)


def cmd_best10(config: AppConfig, *, mode: str = "mtproto", limit: int | None = None):
    from exporter.exporter import eligible_results
    from scan import mode_folder

    results = run_pipeline(config, export=True, mode=mode, limit=limit)
    # Display exactly the candidates eligible for the saved best10 links file.
    folder = mode_folder(config, mode)
    print_best10(eligible_results(results, config.output), str(folder), mode=mode)
    return not getattr(results, "interrupted", False)


def cmd_dashboard(config: AppConfig, *, open_browser: bool = True) -> bool:
    """Serve the local dashboard with the standard library, including lite installs."""
    from dashboard.server import run
    from scan import network_folder

    try:
        run(
            config.dashboard.host,
            config.dashboard.port,
            str(network_folder(config)),
            open_browser=open_browser,
        )
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    return True


def cmd_scheduler(config: AppConfig, *, mode: str = "mtproto", limit: int | None = None):
    from scheduler.scheduler import Scheduler

    def scheduled_scan():
        if cmd_best10(config, mode=mode, limit=limit) is False:
            raise KeyboardInterrupt

    Scheduler(config, pipeline_fn=scheduled_scan).start()


def cmd_top(config: AppConfig, n: int, *, mode: str = "mtproto"):
    from scan import mode_folder

    path = mode_folder(config, mode) / "proxy.json"
    if not path.exists() and mode == "mtproto" and config.scan.network == "default":
        path = Path(config.output.folder) / "proxy.json"  # v2 migration, MTProto only
    if not path.exists():
        print("No exported data yet. Run: python main.py export")
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError(f"Unreadable export: {path}. Run `python main.py export` to regenerate it.") from exc
    if not isinstance(data, list):
        raise ValueError(f"Invalid export format: {path}. Expected a JSON list.")
    if not data:
        print("The last export contains no verified proxies.")
        return
    qualified = []
    for index, proxy in enumerate(data, 1):
        if not isinstance(proxy, dict) or not isinstance(proxy.get("server"), str):
            raise ValueError(f"Invalid proxy entry {index} in {path}; regenerate the export.")
        score = proxy.get("score")
        port = proxy.get("port")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise ValueError(f"Invalid score in export entry {index}; regenerate the export.")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError(f"Invalid port in export entry {index}; regenerate the export.")
        if mode != "web":
            # Use the same structural proof validation as the dashboard so an
            # old saved transport probe cannot become a strict recommendation.
            from dashboard.data import _strict_qualified

            if not _strict_qualified(proxy):
                continue
        qualified.append(proxy)
    if len(qualified) != len(data):
        _message(
            f"Skipped {len(data) - len(qualified)} saved entries without strict Telegram evidence. "
            f"Run: python main.py best10 --mode {mode} --count ALL"
        )
    for index, proxy in enumerate(qualified[:n], 1):
        port, score = proxy["port"], proxy["score"]
        latency = proxy.get("telegram_p95_ms") if mode != "web" else proxy.get("avg_latency_ms")
        metric = "Telegram RTT p95" if mode != "web" else "probe latency"
        print(f"{index:>3}. {proxy['server']}:{port} score={score:.3f} {metric}={latency} ms")
        if mode == "telegram":
            from tester.secrets import decode_secret, normalize_server
            from utils.models import Proxy

            try:
                saved = Proxy(
                    normalize_server(proxy["server"]), port, decode_secret(proxy.get("secret")).encoded
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid Telegram proxy in entry {index}; regenerate the export.") from exc
            print(saved.web_link())


def cmd_json(config: AppConfig, *, mode: str = "mtproto", limit: int | None = None):
    from scan import mode_folder

    cmd_best10(config, mode=mode, limit=limit)
    print(str(mode_folder(config, mode) / "proxy.json"))


def cmd_csv(config: AppConfig, *, mode: str = "mtproto", limit: int | None = None):
    from scan import mode_folder

    cmd_best10(config, mode=mode, limit=limit)
    print(str(mode_folder(config, mode) / "proxy.csv"))


def cmd_history(config: AppConfig, n: int = 20, *, mode: str = "mtproto"):
    from utils.scan_state import HistoryStore

    rows = HistoryStore(Path(config.output.folder) / ".state", config.scan.network, mode).rows(limit=n)
    print(f"\nHISTORY | network={config.scan.network} | mode={mode}")
    if not rows:
        _message("No saved history for this network. Run a scan first.")
    for row in rows:
        _message(json.dumps(row, ensure_ascii=False, sort_keys=True))
    _message("History is past evidence. Run a fresh scan before choosing a connection.")


def _network_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value):
        raise argparse.ArgumentTypeError("Network name: 1-32 letters, digits, underscores or hyphens")
    from config.loader import ScanConfig

    try:
        return ScanConfig(network=value).validate().network
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _menu_mode() -> str | None:
    value = input("Mode: 1) MTProto  2) Telegram Web Link  3) HTTP / SOCKS  0) Back: ").strip()
    if value == "0":
        return None
    if value not in ("1", "2", "3"):
        raise ValueError("Choose mode 1, 2, 3, or 0 to return")
    return {"1": "mtproto", "2": "telegram", "3": "web"}[value]


def _error(exc: Exception, config: AppConfig | None = None):
    logging.getLogger("mtselector").debug("Command failed", exc_info=True)
    _message(f"Error: {exc}")
    if isinstance(exc, ImportError):
        _message("Install the runtime packages: python -m pip install -r requirements-lite.txt")
    if config:
        _message(f"Config: {config._path or CONFIG_PATH}")


def choose_count(mode: str):
    print(f"\n[{mode.upper()}] TEST SCOPE")
    print("1) ALL - test every collected candidate")
    print("2) Custom count - test N candidates")
    print("0) Back")
    while True:
        choice = input("Choose (0-2): ").strip().lower()
        if choice in ("1", "all"):
            return None
        if choice == "0":
            return "back"
        if choice == "2":
            while True:
                value = input("Candidate count (positive integer, 0 = back): ").strip()
                if value == "0":
                    return "back"
                try:
                    return _positive_int(value)
                except argparse.ArgumentTypeError:
                    print("Enter a positive integer.")
        print("Invalid choice. Type 0, 1 (ALL), or 2.")


def interactive_menu(config: AppConfig, *, open_browser: bool = True):
    print_banner()
    while True:
        print("\n1) MTProto - test and show TOP 10")
        print("2) Telegram Web Link - https://t.me/proxy - TOP 10")
        print("3) HTTP / SOCKS - internet proxy - TOP 10")
        print("4) Open local web dashboard")
        print("5) Collect candidates (no testing)")
        print("6) Exit")
        print("7) Resume the last interrupted scan")
        print(f"8) Network profile: {config.scan.network}")
        print("9) View network history")
        print("10) Recheck saved top connections")
        try:
            choice = input("\nChoose (1-10): ").strip()
        except EOFError:
            _message("No keyboard input. Run a direct command, e.g. python main.py best10")
            return
        except KeyboardInterrupt:
            print("\nBye!")
            return
        try:
            if choice in ("1", "2", "3"):
                mode = {"1": "mtproto", "2": "telegram", "3": "web"}[choice]
                count = choose_count(mode)
                if count != "back":
                    cmd_best10(config, mode=mode, limit=count)
            elif choice == "4":
                cmd_dashboard(config, open_browser=open_browser)
            elif choice == "5":
                mode_choice = input(
                    "Collect: 1) MTProto  2) Telegram Web Link  3) HTTP / SOCKS  0) Back: "
                ).strip()
                if mode_choice in ("1", "2", "3"):
                    cmd_collect(config, mode={"1": "mtproto", "2": "telegram", "3": "web"}[mode_choice])
                elif mode_choice != "0":
                    print("Invalid choice. Returning to the menu.")
            elif choice == "6":
                print("Bye!")
                return
            elif choice == "7":
                mode = _menu_mode()
                if mode:
                    resume_choice = input("Resume: 1) Scan  2) Saved-candidate recheck  0) Back: ").strip()
                    if resume_choice == "0":
                        continue
                    if resume_choice not in ("1", "2"):
                        raise ValueError("Choose resume 1, 2, or 0 to return")
                    previous = config.scan.resume
                    try:
                        config.scan.resume = True
                        resume_command = cmd_recheck if resume_choice == "2" else cmd_best10
                        resume_command(config, mode=mode, limit=None)
                    finally:
                        config.scan.resume = previous
            elif choice == "8":
                config.scan.network = _network_name(input("Network name (e.g. wifi, mobile, home): ").strip())
                _message(
                    f"Selected network: {config.scan.network}. Results, checkpoints and history stay separate."
                )
            elif choice == "9":
                mode = _menu_mode()
                if mode:
                    cmd_history(config, mode=mode)
            elif choice == "10":
                mode = _menu_mode()
                if mode:
                    cmd_recheck(config, mode=mode)
            else:
                print("Invalid choice. Type a number from 1 to 10.")
        except KeyboardInterrupt:
            print("\nCancelled. Returning to the menu.")
        except EOFError:
            _message("No keyboard input. Use a direct command with --mode and --count.")
            return
        except Exception as exc:
            _error(exc, config)


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("N must be a positive integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("N must be a positive integer")
    return number


def _candidate_count(value: str) -> int | None:
    return None if value.casefold() == "all" else _positive_int(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, epilog="No command opens the interactive menu.")
    parser.add_argument(
        "command",
        nargs="?",
        default="menu",
        choices=(
            "menu",
            "run",
            "best10",
            "collect",
            "benchmark",
            "export",
            "dashboard",
            "scheduler",
            "top",
            "json",
            "csv",
            "history",
            "selftest",
            "recheck",
        ),
    )
    parser.add_argument(
        "n", nargs="?", type=_positive_int, help="number of proxies to show with 'top' (default: 10)"
    )
    parser.add_argument(
        "--config", type=Path, help="custom YAML config (its relative paths use its containing directory)"
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="show the dashboard URL without opening a browser"
    )
    parser.add_argument("--version", action="version", version=f"REVMAMAD {__version__}")
    parser.add_argument(
        "--mode",
        choices=("mtproto", "telegram", "web"),
        default="mtproto",
        help="mtproto=tg:// links; telegram=https://t.me/proxy links; web=HTTP/SOCKS (default: mtproto)",
    )
    parser.add_argument(
        "--count",
        type=_candidate_count,
        default=None,
        metavar="ALL|N",
        help="candidates to test, not the top10 result count (default: ALL)",
    )
    parser.add_argument(
        "--network", type=_network_name, help="named local network profile, e.g. wifi or mobile"
    )
    parser.add_argument("--resume", action="store_true", help="continue a recent matching checkpoint")
    parser.add_argument(
        "--single-stage", action="store_true", help="strict baseline scan without sustained finalists"
    )
    parser.add_argument(
        "--no-prefilter", action="store_true", help="apply full strict tests to every selected candidate"
    )
    args = parser.parse_args(argv)
    if args.n is not None and args.command not in ("top", "history"):
        parser.error("N is only supported with the 'top' and 'history' commands")
    if args.resume and args.command not in ("menu", "run", "best10", "export", "json", "csv", "recheck"):
        parser.error("--resume requires a saved scan command (best10, run, export, json, csv, or menu)")
    config = None
    try:
        config = load_config(args.config)
        if args.network is not None:
            config.scan.network = args.network
        if args.resume:
            config.scan.resume = True
        if args.single_stage:
            config.scan.strategy = "single"
        if args.no_prefilter:
            config.scan.prefilter = False
        setup_logger(
            name="mtselector", log_dir=config.logging.log_dir, level=getattr(logging, config.logging.level)
        )
        if args.command == "menu":
            interactive_menu(config, open_browser=not args.no_browser)
        elif args.command == "run":
            if args.mode == "mtproto" and args.count is None:
                from exporter.exporter import eligible_results
                from scan import mode_folder

                print_best10(
                    eligible_results(run_pipeline(config, export=True), config.output),
                    str(mode_folder(config, "mtproto")),
                )
            else:
                cmd_best10(config, mode=args.mode, limit=args.count)
        elif args.command == "dashboard":
            cmd_dashboard(config, open_browser=not args.no_browser)
        elif args.command == "top":
            if args.mode == "mtproto":
                cmd_top(config, args.n or 10)
            else:
                cmd_top(config, args.n or 10, mode=args.mode)
        elif args.command == "history":
            cmd_history(config, args.n or 20, mode=args.mode)
        elif args.command == "selftest":
            from utils.selftest import run_selftest

            return run_selftest(config)
        else:
            commands = {
                "best10": cmd_best10,
                "collect": cmd_collect,
                "benchmark": cmd_benchmark,
                "export": cmd_export,
                "scheduler": cmd_scheduler,
                "json": cmd_json,
                "csv": cmd_csv,
                "recheck": cmd_recheck,
            }
            if args.mode == "mtproto" and args.count is None:
                commands[args.command](config)
            else:
                commands[args.command](config, mode=args.mode, limit=args.count)
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    except Exception as exc:
        _error(exc, config)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
