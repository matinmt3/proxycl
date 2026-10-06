"""REVMAMAD v2: collect, verify, rank, and export Telegram MTProto proxies."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import shutil
import sys
import textwrap
from collections import Counter
from pathlib import Path

from config.loader import AppConfig
from utils.logger import setup_logger

__version__ = "2.0.0"
PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


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
    print("Telegram MTProto Proxy Radar")
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


def run_pipeline(config: AppConfig, export: bool = True):
    # Keep HTTP/crypto imports lazy so --help, --version, and the dashboard work
    # independently of the scanner's optional installation state.
    from benchmark.scorer import rank, score_all
    from collector.collector import Collector
    from exporter.exporter import Exporter
    from tester.runner import ParallelTester

    logger = logging.getLogger("mtselector")
    proxies = Collector(config).collect()
    logger.info("Collected %d proxies total.", len(proxies))
    if proxies:
        results = rank(score_all(ParallelTester(config).run(proxies), config.scoring))
    else:
        logger.warning("No proxies collected. Check the source warnings and your config.")
        results = []
    _run_counts(proxies, results)

    if export:
        # An empty fresh run must clear stale proxies from a previous export.
        exporter = Exporter(config.output)
        paths = exporter.export_all(results)
        exporter.export_best_n_readable(results, n=10)
        logger.info("Export complete: %s", paths)
    return results


def print_summary(results, n: int = 10):
    print("\nRANK / SERVER / SCORE / SUCCESS / LATENCY")
    print("-" * min(_width(), 48))
    for i, result in enumerate(results[:n], 1):
        avg = f"{result.avg_latency_ms:.0f} ms" if result.avg_latency_ms is not None else "-"
        print(f"#{i} {result.proxy.server}:{result.proxy.port}")
        _message(f"  Score: {result.score:.3f} | Success: {result.success_rate:.0%} | Latency: {avg}")
    print()


def print_best10(results, output_folder: str):
    """Show up to ten verified proxies; a real scan may return fewer than ten."""
    healthy = [r for r in results if r.success_rate > 0 and r.avg_latency_ms is not None]
    healthy.sort(key=lambda result: result.avg_latency_ms)
    top = healthy[:10]
    print("\n" + "=" * min(_width(), 40))
    print(f"VERIFIED PROXIES: {len(top)} / 10")
    print("=" * min(_width(), 40))
    if len(top) < 10:
        _message(f"Showing {len(top)} verified proxies. Ten is the maximum, not a guaranteed count.")
        _message(
            "Network failures and your export filters can reduce this number. Check the failure counts above and try another source or network."
        )
    for i, result in enumerate(top, 1):
        print(f"\n#{i} {result.proxy.server}:{result.proxy.port}")
        _message(
            f"Speed: {result.avg_latency_ms:.0f} ms | Success: {result.success_rate:.0%} | Score: {result.score:.1%}"
        )
        print(result.proxy.tg_link())
    print()
    _message(f"Saved to: {Path(output_folder) / 'best10_links.txt'}")


def cmd_collect(config: AppConfig):
    from collector.collector import Collector

    print(f"Collected {len(Collector(config).collect())} unique proxy candidates (not yet verified).")


def cmd_benchmark(config: AppConfig):
    print_summary(run_pipeline(config, export=False), n=20)


def cmd_export(config: AppConfig):
    print_summary(run_pipeline(config, export=True), n=10)
    _message(f"All formats exported to: {config.output.folder}")


def cmd_best10(config: AppConfig):
    from exporter.exporter import Exporter

    results = run_pipeline(config, export=True)
    # Display exactly the candidates eligible for the saved best10 links file.
    print_best10(Exporter(config.output)._filtered(results), config.output.folder)


def cmd_dashboard(config: AppConfig, *, open_browser: bool = True) -> bool:
    """Serve the local dashboard with the standard library, including lite installs."""
    from dashboard.server import run

    try:
        run(config.dashboard.host, config.dashboard.port, config.output.folder, open_browser=open_browser)
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    return True


def cmd_scheduler(config: AppConfig):
    from scheduler.scheduler import Scheduler

    Scheduler(config, pipeline_fn=lambda: run_pipeline(config, export=True)).start()


def cmd_top(config: AppConfig, n: int):
    path = Path(config.output.folder) / "proxy.json"
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
    for index, proxy in enumerate(data[:n], 1):
        if not isinstance(proxy, dict) or not isinstance(proxy.get("server"), str):
            raise ValueError(f"Invalid proxy entry {index} in {path}; regenerate the export.")
        score = proxy.get("score")
        port = proxy.get("port")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise ValueError(f"Invalid score in export entry {index}; regenerate the export.")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError(f"Invalid port in export entry {index}; regenerate the export.")
        latency = proxy.get("avg_latency_ms")
        print(f"{index:>3}. {proxy['server']}:{port} score={score:.3f} latency={latency} ms")


def cmd_json(config: AppConfig):
    run_pipeline(config, export=True)
    print(str(Path(config.output.folder) / "proxy.json"))


def cmd_csv(config: AppConfig):
    run_pipeline(config, export=True)
    print(str(Path(config.output.folder) / "proxy.csv"))


def _error(exc: Exception, config: AppConfig | None = None):
    logging.getLogger("mtselector").debug("Command failed", exc_info=True)
    _message(f"Error: {exc}")
    if isinstance(exc, ImportError):
        _message("Install the runtime packages: python -m pip install -r requirements-lite.txt")
    if config:
        _message(f"Config: {config._path or CONFIG_PATH}")


def interactive_menu(config: AppConfig, *, open_browser: bool = True):
    print_banner()
    while True:
        print("\n1) Find up to 10 verified proxies")
        print("2) Full scan and export")
        print("3) Open local web dashboard")
        print("4) Collect candidates (no testing)")
        print("5) Exit")
        try:
            choice = input("\nChoose (1-5): ").strip()
        except EOFError:
            _message("No keyboard input. Run a direct command, e.g. python main.py best10")
            return
        except KeyboardInterrupt:
            print("\nBye!")
            return
        try:
            if choice == "1":
                cmd_best10(config)
            elif choice == "2":
                cmd_export(config)
            elif choice == "3":
                cmd_dashboard(config, open_browser=open_browser)
            elif choice == "4":
                cmd_collect(config)
            elif choice == "5":
                print("Bye!")
                return
            else:
                print("Invalid choice. Type 1, 2, 3, 4, or 5.")
        except KeyboardInterrupt:
            print("\nCancelled. Returning to the menu.")
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
    args = parser.parse_args(argv)
    if args.n is not None and args.command != "top":
        parser.error("N is only supported with the 'top' command")
    config = None
    try:
        config = load_config(args.config)
        setup_logger(
            name="mtselector", log_dir=config.logging.log_dir, level=getattr(logging, config.logging.level)
        )
        if args.command == "menu":
            interactive_menu(config, open_browser=not args.no_browser)
        elif args.command == "run":
            print_summary(run_pipeline(config, export=True), n=10)
        elif args.command == "dashboard":
            cmd_dashboard(config, open_browser=not args.no_browser)
        elif args.command == "top":
            cmd_top(config, args.n or 10)
        else:
            commands = {
                "best10": cmd_best10,
                "collect": cmd_collect,
                "benchmark": cmd_benchmark,
                "export": cmd_export,
                "scheduler": cmd_scheduler,
                "json": cmd_json,
                "csv": cmd_csv,
            }
            commands[args.command](config)
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    except Exception as exc:
        _error(exc, config)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
