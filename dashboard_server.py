#!/usr/bin/env python3
"""Local dev server for the Replenishment dashboard — WITH real persistence + run endpoints.

Plain `python -m http.server` only serves static files, so the dashboard's "Run
Replenishment" button had nothing real to call, and every Control Center Add/Remove
button (Hold rules, Config rules, Hold-to-Planogram, Additional Orders, the pharmacy
Active toggle) only ever mutated an in-memory array — none of it ever reached disk,
so it was invisible to the next real run and gone on refresh. This server closes
both loops:

  POST /api/run
    1. Merges (upserts, keyed by pharmacy_id+item_code) any LT overrides the browser
       sends into data/lt_override.csv, so edits accumulate as standing rules instead
       of overwriting each other across sessions.
    2. Actually executes run_csv_replenish.py as a subprocess and waits for it to finish.
    3. Returns success/failure (+ stdout/stderr) so the dashboard can reload with the
       genuinely fresh ordering_data.js, or show a real error instead of pretending.

  POST /api/save-list
    Generic "save immediately" endpoint for every other Control Center list (hold.csv,
    config.csv, hold_to_plano.csv, additional_orders.csv, store.csv) — the dashboard
    sends the file name, field order, and the FULL current row list (not a diff — these
    lists are managed by Add/Remove, so the browser's current array IS the source of
    truth), and this does a full overwrite. Whitelisted filenames only.

Usage:
    python dashboard_server.py [port]     (default 8000)
    then open http://localhost:8000/dashboard_preview.html
"""
import csv
import functools
import io
import json
import os
import subprocess
import sys
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
OVERRIDE_PATH = os.path.join(DATA_DIR, "lt_override.csv")
SCRIPT_PATH = os.path.join(ROOT, "run_csv_replenish.py")
OVERRIDE_FIELDS = ["pharmacy_id", "item_code", "lead_time", "updated_by", "updated_at", "reason"]

# Filenames /api/save-list is allowed to write — a hardcoded whitelist so a request
# body can never point this at an arbitrary path on disk.
SAVEABLE_FILES = {"hold.csv", "config.csv", "hold_to_plano.csv", "additional_orders.csv", "store.csv"}


def _read_existing_overrides():
    """{(pharmacy_id, item_code): row_dict} from the current lt_override.csv, if any."""
    rows = {}
    if os.path.exists(OVERRIDE_PATH):
        with open(OVERRIDE_PATH, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                pid = str(row.get("pharmacy_id", "")).strip()
                item = str(row.get("item_code", "")).strip()
                if pid and item:
                    rows[(pid, item)] = row
    return rows


def _merge_and_write_overrides(incoming_csv_text):
    """Upsert incoming rows into the existing override file; return the total row count."""
    existing = _read_existing_overrides()
    if incoming_csv_text and incoming_csv_text.strip():
        for row in csv.DictReader(io.StringIO(incoming_csv_text)):
            pid = str(row.get("pharmacy_id", "")).strip()
            item = str(row.get("item_code", "")).strip()
            if pid and item:
                existing[(pid, item)] = row

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OVERRIDE_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OVERRIDE_FIELDS)
        writer.writeheader()
        for row in existing.values():
            writer.writerow({k: row.get(k, "") for k in OVERRIDE_FIELDS})
    return len(existing)


class Handler(SimpleHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/api/run":
            self._handle_run()
        elif self.path == "/api/save-list":
            self._handle_save_list()
        else:
            self.send_error(404, "Unknown endpoint")

    def _handle_run(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length).decode("utf-8") if length else ""

            override_count = _merge_and_write_overrides(body)

            start = time.perf_counter()
            result = subprocess.run(
                [sys.executable, SCRIPT_PATH],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
            )
            elapsed = round(time.perf_counter() - start, 2)

            payload = {
                "ok": result.returncode == 0,
                "duration": elapsed,
                "overrideRows": override_count,
                "stdout": result.stdout[-4000:],
                "stderr": result.stderr[-4000:],
            }
            self._send_json(200 if payload["ok"] else 500, payload)
        except subprocess.TimeoutExpired:
            self._send_json(500, {"ok": False, "error": "run_csv_replenish.py timed out after 300s"})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_save_list(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length).decode("utf-8") if length else ""
            payload = json.loads(body) if body else {}

            filename = payload.get("file")
            fields = payload.get("fields")
            rows = payload.get("rows")
            if filename not in SAVEABLE_FILES:
                self._send_json(400, {"ok": False, "error": f"'{filename}' is not a saveable file"})
                return
            if not isinstance(fields, list) or not isinstance(rows, list):
                self._send_json(400, {"ok": False, "error": "'fields' and 'rows' must be arrays"})
                return

            os.makedirs(DATA_DIR, exist_ok=True)
            path = os.path.join(DATA_DIR, filename)
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
                for row in rows:
                    writer.writerow({k: row.get(k, "") for k in fields})

            self._send_json(200, {"ok": True, "file": filename, "rows": len(rows)})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def _send_json(self, status, payload):
        body_bytes = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    handler = functools.partial(Handler, directory=ROOT)
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    print(f"Serving {ROOT}")
    print(f"Real pipeline trigger enabled at POST /api/run")
    print(f"Open: http://localhost:{port}/dashboard_preview.html")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
