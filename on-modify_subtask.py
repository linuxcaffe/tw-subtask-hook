#!/usr/bin/env python3
"""
Taskwarrior Subtask Hook - On-Modify
Version: 1.1.0
Date: 2026-02-28

Activates subtask annotations as real dependent tasks when a parent task
is started. Updates parent annotation state when child tasks complete or
are deleted.

Annotation format:
    - [ ] description [key:val …]          # dormant
    - [P] description [key:val …] <uuid>   # Pending (activated, child exists)
    - [C] description [key:val …] <uuid>   # Completed
    - [D] description [key:val …] <uuid>   # Deleted

Inline key:val tokens (e.g. pri:H due:2026-03-01 +tag) override inherited
parent values for that attribute only.

Installation:
    1. Save to ~/.task/hooks/on-modify_subtask.py
    2. chmod +x ~/.task/hooks/on-modify_subtask.py
"""

import os
import sys
import json
import re
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
    """Auto-detect dev vs production mode for log directory."""
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
            log_line = f"{timestamp} [DEBUG-{level}] {message}\n"
            with open(DEBUG_LOG_FILE, "a") as f:
                f.write(log_line)
            print(f"\033[34m[DEBUG-{level}]\033[0m {message}", file=sys.stderr)

    with open(DEBUG_LOG_FILE, "w") as f:
        f.write("=" * 70 + "\n")
        f.write(f"Debug Session - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        try:
            f.write(f"Script: {script_name}\n")
        except Exception:
            pass
        f.write(f"TW_DEBUG Level: {tw_debug_level}\n")
        f.write("=" * 70 + "\n\n")
    debug_log(f"Debug logging initialized: {DEBUG_LOG_FILE}", 1)

else:
    def debug_log(message, level=1):
        pass


# ============================================================================
# Timing support — set TW_TIMING=1 to enable; zero overhead otherwise
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

# Attributes inherited from parent task to child
INHERITED_ATTRS = ('project', 'priority', 'due', 'scheduled', 'until', 'wait')

# Dormant subtask line: - [ ] <content>
# Slightly flexible: allows extra whitespace after the closing bracket.
DORMANT_RE = re.compile(r'^- \[ \]\s+(.+?)$')

# Inline attribute overrides in annotation text: key:value
# Only known inheritable attributes are matched.
INLINE_ATTR_RE = re.compile(
    r'(?<!\S)(pri|priority|project|due|scheduled|until|wait):(\S+)'
)

# Inline tag in annotation text: +word
INLINE_TAG_RE = re.compile(r'(?<!\S)\+(\w+)')


# ============================================================================
# Annotation Content Parsing
# ============================================================================

def parse_annotation_content(content):
    """Parse inline key:val tokens and +tags from annotation content.

    Args:
        content: Text after the '- [ ] ' prefix.

    Returns:
        (clean_description, attrs_dict, tags_list)
        - clean_description: content with inline tokens stripped
        - attrs_dict: {attr: value} overrides ('pri' normalised to 'priority')
        - tags_list: tag strings without leading '+'
    """
    attrs = {}
    tags = []

    for m in INLINE_ATTR_RE.finditer(content):
        key = m.group(1)
        val = m.group(2)
        if key == 'pri':
            key = 'priority'
        attrs[key] = val

    for m in INLINE_TAG_RE.finditer(content):
        tags.append(m.group(1))

    clean = INLINE_ATTR_RE.sub('', content)
    clean = INLINE_TAG_RE.sub('', clean)
    clean = ' '.join(clean.split())  # normalise whitespace

    return clean, attrs, tags


# ============================================================================
# Child Task Creation
# ============================================================================

def create_child_task(annotation_content, parent_task):
    """Create a child task from a dormant subtask annotation.

    Inherits parent attributes and applies any inline overrides. Tags are
    merged (union) between parent and annotation. No circular dep is created:
    only the parent gains a dep on the child (not the reverse).

    Args:
        annotation_content: Full text after '- [ ] ' (may contain inline tokens).
        parent_task: Parent task dictionary.

    Returns:
        child_uuid string, or None on failure.
    """
    clean_desc, inline_attrs, inline_tags = parse_annotation_content(annotation_content)

    cmd = [
        'task', 'rc.hooks=off', 'rc.confirmation=off', 'rc.verbose=new-id',
        'add', clean_desc,
    ]

    # Inherit attributes from parent; inline overrides take precedence.
    for attr in INHERITED_ATTRS:
        if attr in inline_attrs:
            cmd.append(f'{attr}:{inline_attrs[attr]}')
        elif attr in parent_task:
            cmd.append(f'{attr}:{parent_task[attr]}')

    # Merge tags: parent UNION annotation
    parent_tags = parent_task.get('tags', [])
    merged_tags = sorted(set(parent_tags) | set(inline_tags))
    cmd.extend(f'+{tag}' for tag in merged_tags)

    debug_log(f"create_child_task: {cmd}", 2)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        debug_log(f"task add stdout: {result.stdout.strip()}", 2)
    except subprocess.CalledProcessError as e:
        sys.stderr.write(f"[subtask] ERROR: task add failed: {e.stderr}\n")
        debug_log(f"task add failed: {e}", 1)
        return None

    # Extract numeric task ID from "Created task N." output.
    id_match = re.search(r'Created task (\d+)', result.stdout)
    if not id_match:
        sys.stderr.write(
            f"[subtask] WARNING: Could not parse task ID from output: {result.stdout!r}\n"
        )
        return None

    task_id = id_match.group(1)
    debug_log(f"Created task ID: {task_id}", 2)

    # Export the new task to retrieve its UUID.
    try:
        export = subprocess.run(
            ['task', 'rc.hooks=off', 'rc.confirmation=off', task_id, 'export'],
            capture_output=True, text=True, check=False
        )
        tasks_data = json.loads(export.stdout)
        if tasks_data:
            child_uuid = tasks_data[0]['uuid']
            debug_log(f"Child UUID: {child_uuid}", 1)
            return child_uuid
    except (subprocess.SubprocessError, json.JSONDecodeError, KeyError) as e:
        sys.stderr.write(f"[subtask] ERROR: Could not retrieve UUID for task {task_id}: {e}\n")
        debug_log(f"UUID export failed: {e}", 1)

    return None


# ============================================================================
# Case 1: Parent task started — activate subtask annotations
# ============================================================================

def collect_dormant_subtasks(annotations):
    """Scan every line of every annotation for dormant subtask patterns.

    An annotation description may contain multiple lines with arbitrary
    content; only lines matching DORMANT_RE are treated as subtasks.

    Returns:
        List of (ann_idx, line_idx, annotation_content) tuples, in order.
        ann_idx  — index into the annotations list
        line_idx — index of the matching line within that annotation's text
        annotation_content — captured group after '- [ ] '
    """
    results = []
    for ann_idx, ann in enumerate(annotations):
        desc = ann.get('description', '')
        for line_idx, line in enumerate(desc.splitlines()):
            m = DORMANT_RE.match(line.strip())
            if m:
                results.append((ann_idx, line_idx, m.group(1).strip()))
    debug_log(f"collect_dormant_subtasks: found {len(results)}", 2)
    return results


def rewrite_annotation_line(annotations, ann_idx, line_idx, new_line_text):
    """Return a new annotations list with one line replaced in one annotation."""
    ann = dict(annotations[ann_idx])
    lines = ann['description'].splitlines()
    lines[line_idx] = new_line_text
    ann['description'] = '\n'.join(lines)
    updated = list(annotations)
    updated[ann_idx] = ann
    return updated


def handle_parent_started(old_task, new_task):
    """Interactively prompt to activate dormant subtask annotations.

    Scans every line of every annotation for '- [ ] …' patterns — handles
    any number of subtasks across any number of annotations, with arbitrary
    other content on other lines.

    Reads user input from /dev/tty (bypassing hook stdin/stdout).
    Outputs modified new_task JSON with updated annotations and depends list.
    """
    annotations = new_task.get('annotations', [])
    if not annotations:
        debug_log("handle_parent_started: no annotations", 1)
        print(json.dumps(new_task))
        return

    dormant = collect_dormant_subtasks(annotations)
    if not dormant:
        debug_log("handle_parent_started: no dormant subtasks found", 1)
        print(json.dumps(new_task))
        return

    debug_log(f"handle_parent_started: {len(dormant)} dormant subtask(s)", 1)

    # Open /dev/tty so prompts bypass hook stdin/stdout.
    # Use raw OS fd calls — Python's buffered TextIOWrapper calls tell() during
    # init, which raises UnsupportedOperation on character devices.
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
    existing_depends = new_task.get('depends', [])
    if isinstance(existing_depends, str):
        existing_depends = [d for d in existing_depends.split(',') if d]
    new_depends = list(existing_depends)

    activate_all = False

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
                debug_log("User chose quit — stopping activation", 1)
                break
            elif choice == 'n':
                debug_log(f"Skipped: {clean_desc}", 1)
                continue
            elif choice == 'a':
                debug_log("User chose activate-all", 1)
                activate_all = True
                choice = 'y'

            # Activate this subtask line.
            child_uuid = create_child_task(annotation_content, new_task)
            if child_uuid is None:
                sys.stderr.write(
                    f"[subtask] WARNING: Failed to create subtask for '{clean_desc}'\n"
                )
                continue

            # Add child UUID to parent's depends list.
            if child_uuid not in new_depends:
                new_depends.append(child_uuid)

            # Rewrite this line: - [ ] … → - [P] … <child_uuid>
            new_line = f'- [P] {annotation_content} {child_uuid}'
            updated_annotations = rewrite_annotation_line(
                updated_annotations, ann_idx, line_idx, new_line
            )

            tty_write(f'[subtask] → Created child {child_uuid[:8]}… "{clean_desc}"\n')
            debug_log(f"Activated: '{clean_desc}' → {child_uuid}", 1)

    finally:
        os.close(tty_fd)

    new_task['annotations'] = updated_annotations
    if new_depends:
        new_task['depends'] = new_depends
    elif 'depends' in new_task and not new_task['depends']:
        del new_task['depends']

    print(json.dumps(new_task))


# ============================================================================
# Case 2: Child task completed or deleted — update parent annotation
# ============================================================================

def find_parent_task(child_uuid):
    """Search pending/waiting tasks for a [P] annotation line referencing child_uuid.

    Scans every line of every annotation so subtask markers embedded in
    multi-line annotation text are found correctly.

    Returns:
        (parent_task_dict, ann_idx, line_idx) or (None, None, None)
    """
    try:
        result = subprocess.run(
            ['task', 'rc.hooks=off', 'rc.confirmation=off', 'export'],
            capture_output=True, text=True, check=False
        )
        if not result.stdout.strip():
            debug_log("find_parent_task: empty export result", 1)
            return None, None, None

        all_tasks = json.loads(result.stdout)
        debug_log(f"find_parent_task: searching {len(all_tasks)} tasks for {child_uuid}", 2)

        for task in all_tasks:
            if task.get('uuid') == child_uuid:
                continue
            for ann_idx, ann in enumerate(task.get('annotations', [])):
                desc = ann.get('description', '')
                for line_idx, line in enumerate(desc.splitlines()):
                    if '[P]' in line and child_uuid in line:
                        debug_log(
                            f"Found parent: {task['uuid']} ann[{ann_idx}] line[{line_idx}]", 1
                        )
                        return task, ann_idx, line_idx

    except (subprocess.SubprocessError, json.JSONDecodeError) as e:
        sys.stderr.write(f"[subtask] ERROR searching for parent task: {e}\n")
        debug_log(f"find_parent_task error: {e}", 1)

    debug_log(f"find_parent_task: no parent found for {child_uuid}", 1)
    return None, None, None


def handle_child_status_changed(old_task, new_task):
    """Update parent annotation when a child task is completed or deleted.

    Side-effects: modifies parent task via 'task import'.
    Outputs new_task JSON unchanged on stdout.
    """
    child_uuid = new_task['uuid']
    new_status = new_task['status']
    marker = 'C' if new_status == 'completed' else 'D'

    debug_log(f"handle_child_status_changed: {child_uuid} → [{marker}]", 1)

    parent_task, ann_idx, line_idx = find_parent_task(child_uuid)
    if parent_task is None:
        debug_log("No parent found — passing through unchanged", 1)
        print(json.dumps(new_task))
        return

    # Mutate the specific line in the annotation: [P] → [C] or [D]
    annotations = [dict(a) for a in parent_task.get('annotations', [])]
    lines = annotations[ann_idx]['description'].splitlines()
    old_line = lines[line_idx]
    lines[line_idx] = old_line.replace('[P]', f'[{marker}]', 1)
    annotations[ann_idx]['description'] = '\n'.join(lines)
    parent_task['annotations'] = annotations

    debug_log(
        f"Updating parent annotation line:\n  old: {old_line}\n  new: {lines[line_idx]}",
        1
    )

    # Import modified parent back into Taskwarrior.
    try:
        import_data = json.dumps([parent_task])
        result = subprocess.run(
            ['task', 'rc.hooks=off', 'rc.confirmation=off', 'import'],
            input=import_data,
            capture_output=True, text=True, check=False
        )
        debug_log(f"task import rc={result.returncode}", 2)
        if result.returncode != 0:
            sys.stderr.write(
                f"[subtask] WARNING: Failed to update parent annotation: {result.stderr}\n"
            )
    except subprocess.SubprocessError as e:
        sys.stderr.write(f"[subtask] ERROR updating parent annotation: {e}\n")
        debug_log(f"import error: {e}", 1)

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
        f"main: uuid={new_task.get('uuid', '?')} "
        f"old_status={old_task.get('status')} new_status={new_task.get('status')} "
        f"start_event={'start' not in old_task and 'start' in new_task}",
        1
    )

    # Case 1: Parent task started (start key absent in old, present in new).
    if 'start' not in old_task and 'start' in new_task:
        debug_log("Case 1: parent task started", 1)
        handle_parent_started(old_task, new_task)
        return

    # Case 2: Status changed to completed or deleted (child finished).
    old_status = old_task.get('status')
    new_status = new_task.get('status')
    if old_status != new_status and new_status in ('completed', 'deleted'):
        debug_log("Case 2: status changed to completed/deleted", 1)
        handle_child_status_changed(old_task, new_task)
        return

    # No matching case — pass through unchanged.
    debug_log("No matching case, passing through", 2)
    print(json.dumps(new_task))


if __name__ == '__main__':
    main()
