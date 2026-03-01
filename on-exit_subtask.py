#!/usr/bin/env python3
import os as _os_timing, time as _time_module
if _os_timing.environ.get('TW_TIMING'):
    import atexit as _atexit
    _t0 = _time_module.perf_counter()

    def _report_timing():
        elapsed = (_time_module.perf_counter() - _t0) * 1000
        import os.path as _osp
        print(f"[timing] {_osp.basename(__file__)}: {elapsed:.1f}ms", file=__import__('sys').stderr)

    _atexit.register(_report_timing)

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

# ============================================================================
# Constants
# ============================================================================

PENDING_FILE = Path.home() / '.task' / 'subtask_pending.json'

# Annotation-update file: queued [P]→[C/D] rewrites from on-modify
UPDATE_PENDING_FILE = Path.home() / '.task' / 'subtask_update_pending.json'

# Registry of child UUIDs — lets on-modify skip Case 2 for unrelated tasks
SUBTASK_REGISTRY = Path.home() / '.task' / 'config' / 'subtask_registry.json'


# ============================================================================
# Annotation update (Case 2: child completed or deleted)
# ============================================================================

def apply_annotation_updates(updates):
    """Find parent tasks and rewrite [P] → [C] or [D] annotation markers.

    Called after TW has committed all modifications to disk, so our import
    here is the final write and won't be overwritten by TW's in-memory state.
    """
    try:
        result = subprocess.run(
            ['task', 'rc.hooks=off', 'rc.confirmation=off',
             'rc.verbose=nothing', 'rc.context=', 'export'],
            capture_output=True, text=True, check=False
        )
        all_tasks = json.loads(result.stdout) if result.stdout.strip() else []
    except (subprocess.SubprocessError, json.JSONDecodeError) as e:
        sys.stderr.write(f"[subtask] ERROR exporting for annotation update: {e}\n")
        return

    debug_log(f"apply_annotation_updates: {len(updates)} update(s), {len(all_tasks)} tasks", 1)

    for update in updates:
        short_uuid = update.get('child_short', '')
        marker     = update.get('marker', 'C')
        if not short_uuid:
            continue

        parent_task = ann_idx = line_idx = None
        for task in all_tasks:
            for a_idx, ann in enumerate(task.get('annotations', [])):
                for l_idx, line in enumerate(ann.get('description', '').splitlines()):
                    if '[P]' in line and short_uuid in line:
                        parent_task, ann_idx, line_idx = task, a_idx, l_idx
                        break
                if parent_task:
                    break
            if parent_task:
                break

        if parent_task is None:
            sys.stderr.write(f"[subtask] WARNING: no parent found for {short_uuid}\n")
            continue

        annotations = [dict(a) for a in parent_task.get('annotations', [])]
        lines = annotations[ann_idx]['description'].splitlines()
        old_line = lines[line_idx]
        lines[line_idx] = old_line.replace('[P]', f'[{marker}]', 1)
        annotations[ann_idx]['description'] = '\n'.join(lines)
        parent_task['annotations'] = annotations

        debug_log(f"Updating: {old_line!r} → {lines[line_idx]!r}", 1)

        result = subprocess.run(
            ['task', 'rc.hooks=off', 'rc.confirmation=off',
             'rc.verbose=nothing', 'import'],
            input=json.dumps([parent_task]),
            capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            sys.stderr.write(
                f"[subtask] WARNING: annotation import failed: {result.stderr.strip()}\n"
            )
        else:
            debug_log(f"Updated parent annotation [{marker}] for {short_uuid}", 1)
            # Remove from registry — child is done, no future updates needed.
            try:
                reg = json.loads(SUBTASK_REGISTRY.read_text()) if SUBTASK_REGISTRY.exists() else []
                reg = [u for u in reg if not u.startswith(short_uuid)]
                SUBTASK_REGISTRY.write_text(json.dumps(reg))
            except Exception:
                pass


# ============================================================================
# Main
# ============================================================================

def main():
    # --- Process annotation updates (Case 2) ---
    if UPDATE_PENDING_FILE.exists():
        try:
            raw = UPDATE_PENDING_FILE.read_text()
            UPDATE_PENDING_FILE.unlink()
            updates = json.loads(raw)
        except Exception as e:
            debug_log(f"Failed to read update pending file: {e}", 1)
            updates = []
        if updates:
            apply_annotation_updates(updates)

    # --- Process new child task creations (Case 1) ---
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

    parent_uuids = set()

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
            parent_uuids.add(parent_uuid)

        # Register child UUID so on-modify recognises it for Case 2.
        try:
            reg = json.loads(SUBTASK_REGISTRY.read_text()) if SUBTASK_REGISTRY.exists() else []
            if child_uuid not in reg:
                reg.append(child_uuid)
                SUBTASK_REGISTRY.write_text(json.dumps(reg))
        except Exception as e:
            debug_log(f"Could not update subtask registry: {e}", 2)

        print(f'[subtask] -> Created task {task_id} "{desc}"')

    # Stop parent tasks — they are now blocked by their new children.
    for parent_uuid in parent_uuids:
        subprocess.run(
            ['task', 'rc.hooks=off', 'rc.confirmation=off',
             'rc.verbose=nothing', parent_uuid, 'stop'],
            capture_output=True, check=False
        )
        debug_log(f"Stopped parent {parent_uuid}", 1)


if __name__ == '__main__':
    main()
