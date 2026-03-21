#!/usr/bin/env python3
import os as _os_timing, time as _time_module
if _os_timing.environ.get('TW_TIMING'):
    import atexit as _atexit
    _t0 = _time_module.perf_counter()

    def _report_timing(_f=__file__):
        elapsed = (_time_module.perf_counter() - _t0) * 1000
        import os.path as _osp
        print(f"[timing] {_osp.basename(_f)}: {elapsed:.1f}ms", file=__import__('sys').stderr)

    _atexit.register(_report_timing)

"""
Taskwarrior Subtask Hook - On-Modify
Version: 1.4.0
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
import termios
import tty as ttymod
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

# Annotation-update file: queues [P]→[C/D] rewrites for on-exit
UPDATE_PENDING_FILE = Path.home() / '.task' / 'subtask_update_pending.json'

# Registry of child UUIDs created by this hook — used to filter Case 2
SUBTASK_REGISTRY = Path.home() / '.task' / 'config' / 'subtask_registry.json'

# Config file for subtask settings (subtask.end.alert, etc.)
CONFIG_FILE = Path.home() / '.task' / 'config' / 'subtask.rc'

# Any incomplete subtask annotation: dormant [ ] or active [P]
INCOMPLETE_RE = re.compile(r'- \[[ P]\]')

# Pending annotation with trailing uuid8: '- [P] desc ... abcd1234'
PENDING_ANN_RE = re.compile(r'^- \[P\]\s+(.+?)\s+([0-9a-f]{8})\s*$')

# Subprocess base — hooks off, no confirmation, silent
TASK_BASE = ['task', 'rc.hooks=off', 'rc.confirmation=off', 'rc.verbose=nothing']


# ============================================================================
# Terminal I/O helpers
# ============================================================================

def getch():
    """Read one character from /dev/tty in raw mode."""
    try:
        with open('/dev/tty', 'r') as tty:
            fd = tty.fileno()
            old = termios.tcgetattr(fd)
            try:
                ttymod.setraw(fd)
                ch = tty.read(1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            return ch
    except Exception:
        return 'q'


def prompt_key(msg):
    sys.stderr.write(msg)
    sys.stderr.flush()
    ch = getch()
    sys.stderr.write(ch + '\n')
    sys.stderr.flush()
    return ch


def task_run(*args):
    return subprocess.run(TASK_BASE + list(args), capture_output=True, text=True)


# ============================================================================
# Registry helpers
# ============================================================================

def load_registry():
    try:
        if SUBTASK_REGISTRY.exists():
            return json.loads(SUBTASK_REGISTRY.read_text())
    except Exception:
        pass
    return []


def save_registry(reg):
    try:
        SUBTASK_REGISTRY.write_text(json.dumps(reg, indent=2))
    except Exception as e:
        sys.stderr.write(f"[subtask] ERROR saving registry: {e}\n")


# ============================================================================
# Case 3: Parent task completed or deleted — interactive subtask cascade
# ============================================================================

def find_incomplete_subtasks(task, registry):
    """Scan annotations for dormant (- [ ]) and pending (- [P]) subtasks.

    Returns list of dicts:
      type       'pending' | 'dormant'
      desc       human description
      full_uuid  UUID string if found in registry, else None  (pending only)
    """
    reg_by_short = {u[:8]: u for u in registry}
    results = []
    for ann in task.get('annotations', []):
        for line in ann.get('description', '').splitlines():
            line = line.strip()
            m = PENDING_ANN_RE.match(line)
            if m:
                short = m.group(2)
                results.append({
                    'type':      'pending',
                    'desc':      m.group(1).rsplit(None, 1)[0] if ' ' in m.group(1) else m.group(1),
                    'full_uuid': reg_by_short.get(short),
                })
            elif INCOMPLETE_RE.match(line):
                results.append({'type': 'dormant', 'desc': line[6:].strip(), 'full_uuid': None})
    return results


def handle_parent_ending(old_task, new_task):
    """Interactive cascade when a parent task is completed or deleted.

    Finds incomplete subtasks (- [ ] dormant, - [P] pending) and offers
    Y/n/a/q per pending subtask. Dormant subtasks (annotation-only, no child
    task created) are listed as context but require no action.

    If incomplete subtasks found: handles them, prints new_task, sys.exit(0).
    If none found: returns silently (falls through to other cases).
    """
    registry = load_registry()
    incomplete = find_incomplete_subtasks(old_task, registry)
    if not incomplete:
        return

    action      = 'Completing' if new_task.get('status') == 'completed' else 'Deleting'
    parent_desc = old_task.get('description', '?')
    parent_uuid = old_task.get('uuid', '')
    parent_id   = old_task.get('id') or '?'
    total       = len(incomplete)
    noun        = 'subtask' if total == 1 else 'subtasks'

    sys.stderr.write(f"\n[subtask] {action} '{parent_desc}' — {total} incomplete {noun}:\n")
    for s in incomplete:
        marker = '- [P]' if s['type'] == 'pending' else '- [ ]'
        note   = '' if s['type'] == 'pending' else '  (annotation only — no task created)'
        sys.stderr.write(f"  {marker} {s['desc']}{note}\n")
    sys.stderr.write('\n')

    # Collect decisions for pending subtasks (dormant have no child task to act on)
    actions = []
    state   = {'accept_all': False, 'aborted': False}

    for s in incomplete:
        if s['type'] == 'dormant':
            continue
        if state['aborted']:
            break

        if state['accept_all']:
            decision = 'Y'
            sys.stderr.write(f"  Delete '{s['desc']}'? [Y/n/a/q] Y\n")
        else:
            decision = None
            while decision is None:
                ch = prompt_key(f"  Delete '{s['desc']}'? [Y/n/a/q] ")
                if ch in ('Y', 'y', '\r', '\n', ' '):
                    decision = 'Y'
                elif ch in ('n', 'N'):
                    decision = 'n'
                elif ch in ('a', 'A'):
                    decision = 'Y'
                    state['accept_all'] = True
                elif ch in ('q', 'Q', '\x03', '\x1b'):
                    state['aborted'] = True
                    break

        if not state['aborted'] and decision:
            actions.append((s, decision))

    if state['aborted']:
        sys.stderr.write('[subtask] Aborted.\n')
        print(json.dumps(old_task))
        sys.exit(1)

    # Primary task asked last
    action_lower = action.lower()
    if state['accept_all']:
        sys.stderr.write(f"  {action} [{parent_id}] '{parent_desc}' (primary)? Y\n")
        primary_y = True
    else:
        primary_y = None
        while primary_y is None:
            ch = prompt_key(f"  {action} [{parent_id}] '{parent_desc}' (primary)? [Y/n/q] ")
            if ch in ('Y', 'y', '\r', '\n', 'a', 'A', ' '):
                primary_y = True
            elif ch in ('n', 'N'):
                primary_y = False
            elif ch in ('q', 'Q', '\x03', '\x1b'):
                sys.stderr.write('[subtask] Aborted.\n')
                print(json.dumps(old_task))
                sys.exit(1)

    if not primary_y:
        sys.stderr.write(f'[subtask] {action} cancelled.\n')
        print(json.dumps(old_task))
        sys.exit(1)

    # Execute collected actions
    sys.stderr.write('\n')
    deleted_count = 0
    for (s, decision) in actions:
        full_uuid = s['full_uuid']
        desc      = s['desc']
        if decision == 'Y':
            if full_uuid:
                task_run(full_uuid, 'delete')
                sys.stderr.write(f"[subtask] Deleted '{desc}'\n")
                deleted_count += 1
        else:  # 'n' — keep as standalone, remove from registry
            sys.stderr.write(f"[subtask] Kept '{desc}' as standalone task\n")
        if full_uuid and full_uuid in registry:
            registry.remove(full_uuid)

    save_registry(registry)
    if deleted_count or actions:
        sys.stderr.write(f"[subtask] {deleted_count} subtask(s) deleted\n")

    # Strip depends so TW doesn't trigger its own chain-fix prompt
    out = dict(new_task)
    out.pop('depends', None)
    print(json.dumps(out))
    sys.exit(0)


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
# Case 2: Child task completed or deleted — queue annotation update for on-exit
# ============================================================================

def handle_child_status_changed(old_task, new_task):
    """Queue [P]→[C/D] annotation rewrite for on-exit.

    We cannot do the task import here: when 'task delete' runs, TW also
    modifies the parent in-memory (to remove the dep), then calls on-modify
    for the parent. That second hook call outputs TW's stale in-memory parent
    (still [P]) which overwrites our import. Queuing for on-exit avoids this.
    """
    child_uuid = new_task['uuid']
    marker = 'C' if new_task['status'] == 'completed' else 'D'

    debug_log(f"handle_child_status_changed: {child_uuid[:8]} → [{marker}]", 1)

    try:
        existing = []
        if UPDATE_PENDING_FILE.exists():
            existing = json.loads(UPDATE_PENDING_FILE.read_text())
        existing.append({'child_short': child_uuid[:8], 'marker': marker})
        UPDATE_PENDING_FILE.write_text(json.dumps(existing, indent=2))
        debug_log(f"Queued [{marker}] update for {child_uuid[:8]}", 1)
    except (IOError, json.JSONDecodeError) as e:
        sys.stderr.write(f"[subtask] ERROR writing update file: {e}\n")

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

    old_status = old_task.get('status')
    new_status = new_task.get('status')

    debug_log(
        f"uuid={new_task.get('uuid','?')} "
        f"old_status={old_status} new_status={new_status} "
        f"start_event={'start' not in old_task and 'start' in new_task}",
        1
    )

    # Case 3: parent task being completed or deleted — interactive subtask cascade
    if (old_status not in ('completed', 'deleted')
            and new_status in ('completed', 'deleted')):
        handle_parent_ending(old_task, new_task)  # exits if subtasks found; returns if none

    # Case 1: parent task started
    if 'start' not in old_task and 'start' in new_task:
        debug_log("Case 1: parent started", 1)
        handle_parent_started(old_task, new_task)
        return

    # Case 2: child task completed or deleted — only for our subtasks
    if old_status != new_status and new_status in ('completed', 'deleted'):
        child_uuid = new_task.get('uuid', '')
        try:
            reg = json.loads(SUBTASK_REGISTRY.read_text()) if SUBTASK_REGISTRY.exists() else []
        except Exception:
            reg = []
        if child_uuid in reg:
            debug_log("Case 2: status → completed/deleted", 1)
            handle_child_status_changed(old_task, new_task)
            return
        debug_log(f"Case 2 skipped: {child_uuid[:8]} not in subtask registry", 2)

    debug_log("No matching case, passing through", 2)
    print(json.dumps(new_task))


if __name__ == '__main__':
    main()
