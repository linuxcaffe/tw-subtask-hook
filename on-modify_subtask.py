#!/usr/bin/env python3
"""
Taskwarrior Subtask Hook - On-Modify
Version: 1.2.0
Date: 2026-03-01

Interactive prompting when a parent task is started. Marks dormant
subtask annotations as [P] (pending), enriches them with inherited parent
attributes, and queues child task creation for on-exit (after TW commits).

Also handles [P] → [C]/[D] annotation updates when a child task is
completed or deleted.

Annotation format:
    - [ ] description [key:val …]                         # dormant
    - [P] description [inherited attrs] [tags] <uuid>     # Pending
    - [C] description [inherited attrs] [tags] <uuid>     # Completed
    - [D] description [inherited attrs] [tags] <uuid>     # Deleted

Installation:
    Save to ~/.task/hooks/on-modify_subtask.py  (chmod +x)
    Save on-exit_subtask.py to ~/.task/hooks/   (chmod +x)
"""

import os
import sys
import json
import re
import uuid as _uuid_module
import subprocess
from datetime import datetime
from pathlib import Path

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
        script_name = Path(sys.argv[0]).stem if sys.argv else "script"
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
    debug_log(f"Debug logging initialized: {DEBUG_LOG_FILE}", 1)

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

# Attributes inherited from parent task to child, in display order
INHERITED_ATTRS = ('priority', 'project', 'due', 'scheduled', 'until', 'wait')

# Abbreviated names for inherited attrs in the enriched annotation
ATTR_ABBREV = {
    'priority': 'pri',
    'project':  'proj',
    'due':      'due',
    'scheduled':'sched',
    'until':    'until',
    'wait':     'wait',
}

# Dormant subtask line: - [ ] <content>
DORMANT_RE = re.compile(r'^- \[ \]\s+(.+?)$')

# Inline attribute overrides in annotation text
INLINE_ATTR_RE = re.compile(
    r'(?<!\S)(pri|priority|project|due|scheduled|until|wait):(\S+)'
)

# Inline tag in annotation text: +word
INLINE_TAG_RE = re.compile(r'(?<!\S)\+(\w+)')

# Pending-tasks file written by on-modify, consumed by on-exit
PENDING_FILE = Path.home() / '.task' / 'subtask_pending.json'


# ============================================================================
# Annotation Content Parsing
# ============================================================================

def parse_annotation_content(content):
    """Parse inline key:val tokens and +tags from annotation content.

    Returns:
        (clean_description, attrs_dict, tags_list)
    """
    attrs = {}
    tags = []

    for m in INLINE_ATTR_RE.finditer(content):
        key, val = m.group(1), m.group(2)
        if key == 'pri':
            key = 'priority'
        attrs[key] = val

    for m in INLINE_TAG_RE.finditer(content):
        tags.append(m.group(1))

    clean = INLINE_ATTR_RE.sub('', content)
    clean = INLINE_TAG_RE.sub('', clean)
    clean = ' '.join(clean.split())

    return clean, attrs, tags


# ============================================================================
# Child Task Building
# ============================================================================

def build_child_task(annotation_content, parent_task, child_uuid):
    """Build child task dict and enriched annotation content.

    Inherits parent attributes; inline annotation tokens override parent
    values. Tags are merged (parent UNION annotation). The enriched
    annotation folds all resolved attributes back into the text so the
    full context is visible without opening the task.

    Returns:
        (task_dict, enriched_annotation_content)
        task_dict: ready for 'task import'
        enriched_annotation_content: text to follow '- [P] '
    """
    clean_desc, inline_attrs, inline_tags = parse_annotation_content(annotation_content)

    now = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
    task = {
        'uuid':        child_uuid,
        'description': clean_desc,
        'status':      'pending',
        'entry':       now,
    }

    enriched = [clean_desc]

    for attr in INHERITED_ATTRS:
        val = inline_attrs.get(attr) or parent_task.get(attr)
        if val:
            task[attr] = val
            enriched.append(f'{ATTR_ABBREV.get(attr, attr)}:{val}')

    # Merge tags: parent UNION annotation
    parent_tags = parent_task.get('tags', [])
    merged_tags = sorted(set(parent_tags) | set(inline_tags))
    if merged_tags:
        task['tags'] = merged_tags
        enriched.extend(f'+{t}' for t in merged_tags)

    # Short UUID appended for Case-2 parent lookup (8 chars, unique enough)
    enriched.append(child_uuid[:8])

    return task, ' '.join(enriched)


# ============================================================================
# Annotation line helpers
# ============================================================================

def collect_dormant_subtasks(annotations):
    """Scan every line of every annotation for dormant subtask patterns.

    Returns list of (ann_idx, line_idx, annotation_content).
    """
    results = []
    for ann_idx, ann in enumerate(annotations):
        for line_idx, line in enumerate(ann.get('description', '').splitlines()):
            m = DORMANT_RE.match(line.strip())
            if m:
                results.append((ann_idx, line_idx, m.group(1).strip()))
    debug_log(f"collect_dormant_subtasks: {len(results)} found", 2)
    return results


def rewrite_annotation_line(annotations, ann_idx, line_idx, new_line_text):
    """Return a new annotations list with one specific line replaced."""
    ann = dict(annotations[ann_idx])
    lines = ann['description'].splitlines()
    lines[line_idx] = new_line_text
    ann['description'] = '\n'.join(lines)
    updated = list(annotations)
    updated[ann_idx] = ann
    return updated


# ============================================================================
# Case 1: Parent task started — queue subtask activation
# ============================================================================

def handle_parent_started(old_task, new_task):
    """Interactively prompt to activate dormant subtask annotations.

    Pre-generates child UUIDs, builds enriched annotations, and writes
    pending task data to PENDING_FILE for on-exit_subtask.py to import
    after TW commits the parent modification to disk.
    """
    annotations = new_task.get('annotations', [])
    dormant = collect_dormant_subtasks(annotations)

    if not dormant:
        debug_log("handle_parent_started: no dormant subtasks", 1)
        print(json.dumps(new_task))
        return

    debug_log(f"handle_parent_started: {len(dormant)} dormant subtask(s)", 1)

    # Open /dev/tty via raw fd — TextIOWrapper calls tell() on init which
    # raises UnsupportedOperation on character devices.
    try:
        tty_fd = os.open('/dev/tty', os.O_RDWR | os.O_NOCTTY)
    except OSError as e:
        sys.stderr.write(
            f"[subtask] Cannot open /dev/tty: {e} — skipping subtask activation\n"
        )
        print(json.dumps(new_task))
        return

    def tty_write(s):
        os.write(tty_fd, s.encode())

    def tty_readline():
        chars = []
        while True:
            c = os.read(tty_fd, 1).decode('utf-8', errors='replace')
            if c in ('\n', '\r', ''):
                break
            chars.append(c)
        return ''.join(chars)

    updated_annotations = list(annotations)
    pending_tasks = []
    activate_all = False
    parent_uuid = new_task['uuid']

    try:
        for ann_idx, line_idx, annotation_content in dormant:
            clean_desc, _, _ = parse_annotation_content(annotation_content)

            if activate_all:
                choice = 'y'
            else:
                tty_write(f'[subtask] Activate "{clean_desc}"? [Y/n/a/q] ')
                raw_input = tty_readline().strip().lower()
                choice = raw_input if raw_input else 'y'

            if choice == 'q':
                debug_log("User quit", 1)
                break
            elif choice == 'n':
                debug_log(f"Skipped: {clean_desc}", 1)
                continue
            elif choice == 'a':
                activate_all = True

            # Pre-generate UUID and build child task data
            child_uuid = str(_uuid_module.uuid4())
            child_task, enriched_content = build_child_task(
                annotation_content, new_task, child_uuid
            )

            # Queue for creation in on-exit (after TW commits to disk)
            pending_tasks.append({'parent_uuid': parent_uuid, **child_task})

            # Rewrite annotation line: - [ ] … → - [P] <enriched> <uuid>
            updated_annotations = rewrite_annotation_line(
                updated_annotations, ann_idx, line_idx,
                f'- [P] {enriched_content}'
            )

            tty_write(f'[subtask] → Activating: "{clean_desc}"\n')
            debug_log(f"Queued: '{clean_desc}' uuid={child_uuid}", 1)

    finally:
        os.close(tty_fd)

    if pending_tasks:
        try:
            PENDING_FILE.write_text(json.dumps(pending_tasks, indent=2))
            debug_log(f"Wrote {len(pending_tasks)} pending task(s) to {PENDING_FILE}", 1)
        except IOError as e:
            sys.stderr.write(f"[subtask] ERROR writing pending file: {e}\n")

    new_task['annotations'] = updated_annotations
    print(json.dumps(new_task))


# ============================================================================
# Case 2: Child task completed or deleted — update parent annotation
# ============================================================================

def find_parent_task(child_uuid):
    """Search pending/waiting tasks for a [P] annotation line with child_uuid.

    Returns (parent_task_dict, ann_idx, line_idx) or (None, None, None).
    """
    short_uuid = child_uuid[:8]
    try:
        result = subprocess.run(
            ['task', 'rc.hooks=off', 'rc.confirmation=off',
             'rc.verbose=nothing', 'rc.context=', 'export'],
            capture_output=True, text=True, check=False
        )
        if not result.stdout.strip():
            sys.stderr.write(f"[subtask] WARNING: task export returned no output\n")
            return None, None, None

        all_tasks = json.loads(result.stdout)
        debug_log(f"find_parent_task: searching {len(all_tasks)} tasks for {short_uuid}", 2)

        for task in all_tasks:
            if task.get('uuid') == child_uuid:
                continue
            for ann_idx, ann in enumerate(task.get('annotations', [])):
                for line_idx, line in enumerate(ann.get('description', '').splitlines()):
                    if '[P]' in line and short_uuid in line:
                        debug_log(f"Found parent {task['uuid']} ann[{ann_idx}] line[{line_idx}]", 1)
                        return task, ann_idx, line_idx

    except (subprocess.SubprocessError, json.JSONDecodeError) as e:
        sys.stderr.write(f"[subtask] ERROR searching for parent: {e}\n")

    sys.stderr.write(f"[subtask] WARNING: no parent found for child {short_uuid}\n")
    return None, None, None


def handle_child_status_changed(old_task, new_task):
    """Update parent annotation when child is completed or deleted."""
    child_uuid = new_task['uuid']
    marker = 'C' if new_task['status'] == 'completed' else 'D'

    debug_log(f"handle_child_status_changed: {child_uuid} → [{marker}]", 1)

    parent_task, ann_idx, line_idx = find_parent_task(child_uuid)
    if parent_task is None:
        debug_log("No parent found, passing through", 1)
        print(json.dumps(new_task))
        return

    annotations = [dict(a) for a in parent_task.get('annotations', [])]
    lines = annotations[ann_idx]['description'].splitlines()
    old_line = lines[line_idx]
    lines[line_idx] = old_line.replace('[P]', f'[{marker}]', 1)
    annotations[ann_idx]['description'] = '\n'.join(lines)
    parent_task['annotations'] = annotations

    debug_log(f"Updating: {old_line!r} → {lines[line_idx]!r}", 1)

    try:
        result = subprocess.run(
            ['task', 'rc.hooks=off', 'rc.confirmation=off', 'import'],
            input=json.dumps([parent_task]),
            capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            sys.stderr.write(f"[subtask] WARNING: import failed: {result.stderr}\n")
    except subprocess.SubprocessError as e:
        sys.stderr.write(f"[subtask] ERROR: {e}\n")

    print(json.dumps(new_task))


# ============================================================================
# Main
# ============================================================================

def main():
    raw = sys.stdin.read().strip().splitlines()
    if len(raw) < 2:
        sys.stderr.write("[subtask] ERROR: Expected 2 JSON lines on stdin\n")
        if raw:
            print(raw[-1])
        sys.exit(0)

    try:
        old_task = json.loads(raw[0])
        new_task = json.loads(raw[1])
    except json.JSONDecodeError as e:
        sys.stderr.write(f"[subtask] ERROR parsing JSON: {e}\n")
        sys.exit(1)

    debug_log(
        f"uuid={new_task.get('uuid','?')} "
        f"old_status={old_task.get('status')} new_status={new_task.get('status')} "
        f"start_event={'start' not in old_task and 'start' in new_task}",
        1
    )

    # Case 1: parent task started
    if 'start' not in old_task and 'start' in new_task:
        debug_log("Case 1: parent started", 1)
        handle_parent_started(old_task, new_task)
        return

    # Case 2: child task completed or deleted
    old_status = old_task.get('status')
    new_status = new_task.get('status')
    if old_status != new_status and new_status in ('completed', 'deleted'):
        debug_log("Case 2: status → completed/deleted", 1)
        handle_child_status_changed(old_task, new_task)
        return

    debug_log("No matching case, passing through", 2)
    print(json.dumps(new_task))


if __name__ == '__main__':
    main()
