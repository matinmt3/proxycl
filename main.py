"""Telegram MTProto Smart Selector - CLI entrypoint.

Usage:
    python main.py                 # show an interactive menu (easiest for beginners)
    python main.py best10          # collect + test + show/save the 10 fastest working proxies
    python main.py collect         # only collect and print proxy count
    python main.py benchmark       # collect + test, print ranked summary (no export)
    python main.py export          # collect + test + export all formats
    python main.py dashboard       # launch the FastAPI web dashboard
    python main.py scheduler       # run pipeline in the configured scheduler mode
    python main.py top 20          # print the top N proxies from the last export
    python main.py json            # print path to proxy.json (regenerating first)
    python main.py csv             # print path to proxy.csv (regenerating first)
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from benchmark.scorer import rank, score_all
from collector.collector import Collector
from config.loader import AppConfig
from exporter.exporter import Exporter
from scheduler.scheduler import Scheduler
from tester.runner import ParallelTester
from utils.logger import setup_logger

CONFIG_PATH = "config/config.yaml"


def load_config() -> AppConfig:
    return AppConfig.load(CONFIG_PATH)


def run_pipeline(config: AppConfig, export: bool = True):
    logger = logging.getLogger("mtselector")

    collector = Collector(config)
    proxies = collector.collect()
    logger.info("Collected %d proxies total.", len(proxies))
    if not proxies:
        logger.warning("No proxies collected. Check your config/sources.")
        return []

    tester = ParallelTester(config)
    results = tester.run(proxies)

    results = score_all(results, config.scoring)
    results = rank(results)

    if export:
        exporter = Exporter(config.output)
        paths = exporter.export_all(results)
        exporter.export_best_n_readable(results, n=10)
        logger.info("Export complete: %s", paths)

    return results


def print_summary(results, n: int = 10):
    print(f"\n{'RANK':<5}{'SERVER':<22}{'PORT':<7}{'SCORE':<8}{'SUCCESS':<9}{'AVG MS':<9}")
    print("-" * 60)
    for i, r in enumerate(results[:n], 1):
        avg = f"{r.avg_latency_ms:.0f}" if r.avg_latency_ms is not None else "-"
        print(f"{i:<5}{r.proxy.server:<22}{r.proxy.port:<7}{r.score:<8.3f}{r.success_rate:<9.2f}{avg:<9}")
    print()


def print_best10(results, output_folder: str):
    """Print the 10 fastest *working* proxies, sorted purely by speed, with ready-to-tap links."""
    healthy = [r for r in results if r.success_rate > 0 and r.avg_latency_ms is not None]
    healthy.sort(key=lambda r: r.avg_latency_ms)
    top = healthy[:10]

    print("\n" + "=" * 70)
    print("  TOP 10 FASTEST WORKING PROXIES")
    print("=" * 70)

    if not top:
        print("\n  No healthy proxies found this run. Try again in a bit, or add more")
        print("  sources in config/config.yaml — public lists change constantly.\n")
        return

    for i, r in enumerate(top, 1):
        print(f"\n  #{i}  {r.proxy.server}:{r.proxy.port}")
        print(f"      Speed: {r.avg_latency_ms:.0f} ms   |   Success: {r.success_rate * 100:.0f}%   |   Score: {r.score * 100:.1f}%")
        print(f"      Link:  {r.proxy.tg_link()}")

    print("\n" + "-" * 70)
    print(f"  Saved to: {output_folder}/best10_links.txt  (and {output_folder}/top10.json)")
    print("=" * 70 + "\n")


def cmd_collect(config: AppConfig):
    collector = Collector(config)
    proxies = collector.collect()
    print(f"Collected {len(proxies)} unique proxies.")


def cmd_benchmark(config: AppConfig):
    results = run_pipeline(config, export=False)
    print_summary(results, n=20)


def cmd_export(config: AppConfig):
    results = run_pipeline(config, export=True)
    print_summary(results, n=10)
    print(f"All formats exported to: {config.output.folder}/")


def cmd_best10(config: AppConfig):
    results = run_pipeline(config, export=True)
    print_best10(results, config.output.folder)


def cmd_dashboard(config: AppConfig) -> bool:
    """Start the web dashboard. Returns False if its optional packages aren't installed."""
    try:
        import fastapi  # noqa: F401
        import uvicorn
    except ImportError:
        print("\n  The web dashboard needs two extra packages. Install them with:")
        print("      pip install fastapi uvicorn\n")
        return False

    try:
        uvicorn.run(
            "dashboard.app:app",
            host=config.dashboard.host,
            port=config.dashboard.port,
            reload=False,
        )
    except KeyboardInterrupt:
        pass
    return True


def cmd_scheduler(config: AppConfig):
    scheduler = Scheduler(config, pipeline_fn=lambda: run_pipeline(config, export=True))
    scheduler.start()


def cmd_top(config: AppConfig, n: int):
    path = Path(config.output.folder) / "proxy.json"
    if not path.exists():
        print("No exported data found yet. Run `python main.py export` first.")
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    for i, p in enumerate(data[:n], 1):
        print(f"{i:>3}. {p['server']}:{p['port']}  score={p['score']:.3f}  avg_latency={p['avg_latency_ms']}ms")


def cmd_json(config: AppConfig):
    run_pipeline(config, export=True)
    path = Path(config.output.folder) / "proxy.json"
    print(str(path))


def cmd_csv(config: AppConfig):
    run_pipeline(config, export=True)
    path = Path(config.output.folder) / "proxy.csv"
    print(str(path))


def interactive_menu(config: AppConfig):
    """A simple numbered menu for people who don't want to remember CLI flags."""
    while True:
        print("\n" + "=" * 50)
        print("  REVMAMAD — Telegram Proxy Radar")
        print("=" * 50)
        print("  1) Get top 10 fastest working proxies  (recommended)")
        print("  2) Run full pipeline and export all formats")
        print("  3) Open the web dashboard")
        print("  4) Just collect (no testing)")
        print("  5) Exit")
        try:
            choice = input("\n  Choose an option (1-5): ").strip()
        except EOFError:
            print("\n  No keyboard input available. Run a command directly, e.g.:  python main.py best10")
            break

        if choice == "1":
            cmd_best10(config)
        elif choice == "2":
            cmd_export(config)
        elif choice == "3":
            if cmd_dashboard(config):
                break
        elif choice == "4":
            cmd_collect(config)
        elif choice == "5":
            print("Bye!")
            break
        else:
            print("  Invalid choice, please type a number from 1 to 5.")


def main():
    config = load_config()
    setup_logger(name="mtselector", log_dir=config.logging.log_dir, level=getattr(logging, config.logging.level.upper(), logging.INFO))

    args = sys.argv[1:]
    command = args[0] if args else "menu"

    if command == "menu":
        interactive_menu(config)
    elif command == "run":
        results = run_pipeline(config, export=True)
        print_summary(results, n=10)
    elif command == "best10":
        cmd_best10(config)
    elif command == "collect":
        cmd_collect(config)
    elif command == "benchmark":
        cmd_benchmark(config)
    elif command == "export":
        cmd_export(config)
    elif command == "dashboard":
        cmd_dashboard(config)
    elif command == "scheduler":
        cmd_scheduler(config)
    elif command == "top":
        n = int(args[1]) if len(args) > 1 else 10
        cmd_top(config, n)
    elif command == "json":
        cmd_json(config)
    elif command == "csv":
        cmd_csv(config)
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
