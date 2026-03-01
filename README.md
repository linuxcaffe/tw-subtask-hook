# tw-subtask-hook

Taskwarrior hooks that turns dormant subtask annotations into
real dependent tasks — interactively when you start a task, and automatically
updates annotation state when child tasks are completed or deleted.

## Annotation Format

Add subtask annotations to any task using `task <id> annotate`:

```
- [ ] description [key:val …]          # dormant — activated on task start
- [P] description [key:val …] <uuid>   # Pending  (child task exists)
- [C] description [key:val …] <uuid>   # Completed
- [D] description [key:val …] <uuid>   # Deleted
```

Inline `key:val` tokens override the corresponding inherited parent attribute
for that child only. Supported: `pri:`, `priority:`, `project:`, `due:`,
`scheduled:`, `until:`, `wait:`, and `+tag`.

## How It Works

### On `task start`

When you start a task, the hook prompts you for each dormant `- [ ]`
annotation:

```
[subtask] Activate "Stack the wood"? [Y/n/a/q]
```

| Key | Action |
|-----|--------|
| `Y` / Enter | Activate this subtask |
| `n` | Skip (leave as `[ ]`) |
| `a` | Activate this and all remaining without further prompts |
| `q` | Stop; leave remaining annotations dormant |

Activated subtasks become real Taskwarrior tasks inheriting the parent's
attributes, and the parent gains a `dep:` on each child (blocking it until
all children are done).

After activating any subtasks, the parent is automatically `task ID stop`ped

### On child complete or delete

When a child task is marked `done` or `delete`d, the hook automatically
updates the parent annotation marker:

- `done` → `[C]`
- `delete` → `[D]`

No manual annotation management needed.

## Attribute Inheritance

| Attribute | Behaviour |
|-----------|-----------|
| `project` | Inherited from parent |
| `priority` | Inherited from parent |
| `due` | Inherited from parent |
| `scheduled` | Inherited from parent |
| `until` | Inherited from parent |
| `wait` | Inherited from parent |
| `tags` | Parent tags **merged** with annotation tags (union) |
| `annotations` | **Never** inherited (prevents recursive subtask explosion) |

Inline annotation attributes override the parent value for that child only.

## Installation

### Option 1 - download and run the included installer

```bash
curl -fsSL https://raw.githubusercontent.com/linuxcaffe/tw-subtask-hook/main/subtask.install | bash
```
### Option 2 - Via [awesome-taskwarrior](https://github.com/linuxcaffe/awesome-taskwarrior)

```bash
tw -I subtask
```

### Option 3 - Manual (the usual taskwarrior way)

Copy `on-modify_subtask.py` and `on-exit_subtask` to `~/.task/hooks/` and `chmod +x` them.

## Confirm hook status with

```bash
task diag
```

to see them executable inder the Hooks section

## Example

```bash
# Create a parent task
task add "Clean garage" project:home +chores

# Add subtask annotations
task <id> annotate "- [ ] Stack the wood +outdoor"
task <id> annotate "- [ ] Sweep the floor"
task <id> annotate "- [ ] Put away tools pri:H"

# Start the task — hook prompts for each subtask
task <id> start
# [subtask] Activate "Stack the wood"? [Y/n/a/q] Y
# [subtask] → Created child 3f8a1b2c… "Stack the wood"
# [subtask] Activate "Sweep the floor"? [Y/n/a/q] Y
# [subtask] → Created child 7d4e9f1a… "Sweep the floor"
# [subtask] Activate "Put away tools"? [Y/n/a/q] n

# View activated subtasks with `task proj:home +chores list`

# Complete a child task — parent annotation updates automatically
task 3f8a1b2c done
# Parent annotation: - [P] Stack the wood project:home +outdoor +chores → - [C] Stack the wood project:home +outdoor +chores <uuid>
```

## Debugging

```bash
TW_DEBUG=1 task <id> start    # debug log to ~/.task/logs/debug/
TW_TIMING=1 task <id> done    # timing report to stderr
```

## Requirements

- Taskwarrior 2.6.x
- Python 3.6+
- A terminal with `/dev/tty` accessible (required for interactive prompts)

## Notes

- No UDAs required — relationships tracked via native `dep:` and embedded UUIDs
- All subprocess calls use `rc.hooks=off rc.confirmation=off` to prevent recursion
- Interactive prompts use `/dev/tty` directly, bypassing hook stdin/stdout

## License

MIT — see [LICENSE](LICENSE)

## Author

Designed by linuxcaffe, implemented by Claude Sonnet 4.6
