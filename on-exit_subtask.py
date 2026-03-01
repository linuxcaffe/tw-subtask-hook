#!/usr/bin/env python3
"""
Taskwarrior Subtask Hook - On-Exit
Version: 1.2.0
Date: 2026-03-01

Consumes the pending-tasks file written by on-modify_subtask.py and
imports child tasks into Taskwarrior after the parent modification has
been committed to disk.

Runs after every TW command but returns immediately if there is nothing
to do (no pending file).

Installation:
    Save to ~/.task/hooks/on-exit_subtask.py  (chmod +x)
"""

import os
import sys
import json
import subprocess
from pathlib import Path
from datetime import datetime

sys.dont_write_bytecode = True

# ============================================================================
# Debug Infrastructure
# ============================================================================

tw_debug_level = os.environ.get('TW_DEBUG', '0')
try:
    tw_debug_level = int(tw_debug_level)
except ValueError:
    tw_debug_level = 0

debug_active = tw_debug_level >= 1


def get_log_dir():
    cwd = Path.cwd()
    if (cwd / '.git').exists():
        log_dir = cwd / 'logs' / 'debug'
    else:
        log_dir = Path.home() / '.task' / 'logs' / 'debug'
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


if debug_active:
    DEBUG_LOG_DIR = get_log_dir()
    DEBUG_SESSION_ID = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        script_name = Path(__file__).stem
    except Exception:
        script_name = 'on-exit_subtask'
    DEBUG_LOG_FILE = DEBUG_LOG_DIR / f"{script_name}_debug_{DEBUG_SESSION_ID}.log"

    def debug_log(message, level=1):
        if tw_debug_level >= level:
            timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            with open(DEBUG_LOG_FILE, "a") as f:
                f.write(f"{timestamp} [DEBUG-{level}] {message}\n")
            print(f"\033[34m[DEBUG-{level}]\033[0m {message}", file=sys.stderr)

    with open(DEBUG_LOG_FILE, "w") as f:
        f.write("=" * 70 + "\n")
        f.write(f"Debug Session - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"TW_DEBUG Level: {tw_debug_level}\n")
        f.write("=" * 70 + "\n\n")

else:
    def debug_log(message, level=1):
        pass


# ============================================================================
# Timing support
# ============================================================================

if os.environ.get('TW_TIMING'):
    import time as _time_module
    import atexit as _atexit
    _t0 = _time_module.perf_counter()

    def _report_timing():
        elapsed = (_time_module.perf_counter() - _t0) * 1000
        print(f"[timing] {os.path.basename(__file__)}: {elapsed:.1f}ms", file=sys.stderr)

    _atexit.register(_report_timing)


# ============================================================================
# Constants
# ============================================================================

PENDING_FILE = Path.home() / '.task' / 'subtask_pending.json'


# ============================================================================
# Main
# ============================================================================

def main():
    if not PENDING_FILE.exists():
        return

    # Read and immediately remove to prevent duplicate imports on re-run.
    try:
        raw = PENDING_FILE.read_text()
        PENDING_FILE.unlink()
        pending = json.loads(raw)
    except Exception as e:
        debug_log(f"Failed to read pending file: {e}", 1)
        return

    if not pending:
        return

    debug_log(f"Processing {len(pending)} pending child task(s)", 1)

    for item in pending:
        parent_uuid = item.pop('parent_uuid', None)
        child_uuid  = item.get('uuid', '')
        desc        = item.get('description', '?')

        debug_log(f"Importing child: {child_uuid} '{desc}'", 1)

        # Import the child task using the pre-generated UUID.
        try:
            result = subprocess.run(
                ['task', 'rc.hooks=off', 'rc.confirmation=off',
                 'rc.verbose=nothing', 'import'],
                input=json.dumps([item]),
                capture_output=True, text=True, check=False
            )
            if result.returncode != 0:
                sys.stderr.write(
                    f"[subtask] ERROR importing \"{desc}\": {result.stderr.strip()}\n"
                )
                debug_log(f"Import failed rc={result.returncode}: {result.stderr}", 1)
                continue
        except subprocess.SubprocessError as e:
            sys.stderr.write(f"[subtask] ERROR: {e}\n")
            continue

        # Get numeric task ID for the success message.
        task_id = '?'
        try:
            export = subprocess.run(
                ['task', 'rc.hooks=off', child_uuid, 'export'],
                capture_output=True, text=True, check=False
            )
            tasks = json.loads(export.stdout)
            if tasks:
                task_id = tasks[0].get('id', '?')
        except Exception as e:
            debug_log(f"Could not get task ID: {e}", 1)

        # Add dep: parent is blocked by child.
        if parent_uuid:
            subprocess.run(
                ['task', 'rc.hooks=off', 'rc.confirmation=off',
                 'rc.verbose=nothing', parent_uuid, 'modify', f'dep:{child_uuid}'],
                capture_output=True, check=False
            )
            debug_log(f"Added dep: {parent_uuid} blocked by {child_uuid}", 1)

        print(f'[subtask] -> Created task {task_id} "{desc}"')


if __name__ == '__main__':
    main()
