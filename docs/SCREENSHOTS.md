# Screenshots

Place dashboard screenshots here once you have a populated `output/proxy.json`:

- `docs/screenshots/dashboard-overview.png` — top stat cards + latency chart
- `docs/screenshots/dashboard-table.png` — searchable/sortable proxy table

Generate them by running:

```bash
python main.py export
python main.py dashboard
```

then opening `http://localhost:8000` and capturing the page.
