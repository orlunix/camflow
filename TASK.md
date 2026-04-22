Read CLAUDE.md first. Then implement the following.

# CamFlow CLI Specification & Self-Monitoring Implementation

## CLI Commands (final design)

```
camflow run <workflow.yaml>              # Start workflow from beginning (reset state)
camflow resume <workflow.yaml>           # Resume from last checkpoint
camflow resume <workflow.yaml> --from <node>  # Resume from specific node
camflow stop                             # Graceful stop (SIGTERM to engine)
camflow stop --force                     # Force stop (SIGKILL)
camflow status                           # Show engine + workflow status
camflow plan "<request>"                 # Generate workflow.yaml from NL (existing)
camflow evolve report <dir>              # Trace analysis (existing)
camflow scout --type ... --query ...     # Planner scout (existing)
```

## What to Implement

### 1. `camflow stop` (NEW)
In cli_entry/main.py, add stop subcommand:
- Read PID from .camflow/heartbeat.json or .camflow/engine.pid
- Send SIGTERM (default) or SIGKILL (--force)
- Wait up to 10 seconds for process to exit
- Print confirmation

### 2. `camflow status` (NEW)
In cli_entry/main.py, add status subcommand:
- Read .camflow/heartbeat.json → engine alive? PID? last heartbeat?
- Read .camflow/state.json → current node, iteration, completed nodes, status
- Read camc status of current agent (if any)
- Output:

```
Workflow: /path/to/workflow.yaml
Engine:   ALIVE (pid 12345, heartbeat 5s ago)
Node:     eval_swerv (iteration 3, attempt 1)
Agent:    5130c656 (running, 2m 30s)
Progress: 1/4 nodes completed
  [done] eval_vexriscv
  [>>>>] eval_swerv
  [    ] eval_c910
  [    ] comparison
Uptime:   20m 34s
```

Or if dead:
```
Workflow: /path/to/workflow.yaml
Engine:   DEAD (last heartbeat 10m ago, pid 12345 not found)
Node:     eval_swerv (was in progress)
Action:   camflow resume workflow.yaml
```

### 3. Heartbeat Thread (in engine.py)
Add a daemon thread that runs alongside the main engine loop:

```python
import threading, json, time, os

class HeartbeatThread(threading.Thread):
    def __init__(self, camflow_dir, interval=30):
        super().__init__(daemon=True)
        self.camflow_dir = camflow_dir
        self.interval = interval
        self.state_ref = None  # set by engine to share state

    def run(self):
        path = os.path.join(self.camflow_dir, "heartbeat.json")
        while True:
            data = {
                "pid": os.getpid(),
                "timestamp": time.time(),
                "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            if self.state_ref:
                data["pc"] = self.state_ref.get("pc", "")
                data["iteration"] = self.state_ref.get("iteration", 0)
                data["agent_id"] = self.state_ref.get("current_agent_id", "")
                data["status"] = self.state_ref.get("status", "")
            try:
                with open(path + ".tmp", "w") as f:
                    json.dump(data, f)
                os.replace(path + ".tmp", path)  # atomic write
            except Exception:
                pass
            time.sleep(self.interval)
```

Start it in engine.run() before the main loop:
```python
hb = HeartbeatThread(self.camflow_dir)
hb.state_ref = self.state
hb.start()
```

### 4. Lock File (in engine.py)
Prevent two engines on the same workflow:

```python
import fcntl

def _acquire_lock(self):
    self._lock_path = os.path.join(self.camflow_dir, "engine.lock")
    self._lock_fd = open(self._lock_path, "w")
    try:
        fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        print("ERROR: another camflow engine is already running on this workflow")
        print(f"  Lock: {self._lock_path}")
        print(f"  Check: camflow status")
        sys.exit(1)
    self._lock_fd.write(str(os.getpid()))
    self._lock_fd.flush()

def _release_lock(self):
    if hasattr(self, '_lock_fd') and self._lock_fd:
        fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        self._lock_fd.close()
        try:
            os.unlink(self._lock_path)
        except OSError:
            pass
```

### 5. `run` semantics: always fresh start
In engine.py run(), when starting:
- Clear state.json (or create fresh)
- Clear heartbeat.json, engine.lock
- Start from first node in workflow

### 6. `resume` semantics: from checkpoint
Already implemented in cli_entry/resume.py. Just ensure:
- Reads existing state.json
- Does NOT reset it
- Handles orphan agent cleanup
- Acquires lock file
- Starts heartbeat

### 7. Documentation
Write docs/self-monitoring.md with:
- Architecture diagram: camflow calls camc, monitors itself
- CLI reference (all commands)
- Heartbeat mechanism
- Crash recovery flow (detect via heartbeat, recover via resume)
- Lock file prevents double-run
- Example usage session

## Implementation Order
1. heartbeat thread + lock file in engine.py
2. camflow stop in cli_entry/main.py
3. camflow status in cli_entry/main.py
4. docs/self-monitoring.md
5. tests

## Tests
- test_heartbeat: start engine, verify heartbeat.json updates
- test_lock: start two engines, second one fails
- test_stop: start engine, camflow stop, verify graceful shutdown
- test_status: start engine, camflow status shows correct info
- test_status_dead: kill engine, camflow status shows DEAD

Commit after each working piece. Push to origin.
