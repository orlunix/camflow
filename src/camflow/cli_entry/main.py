import argparse
import os
import sys

from camflow.backend.cam.engine import Engine, EngineConfig
from camflow.engine.dsl import load_workflow, validate_workflow


def main():
    parser = argparse.ArgumentParser(
        prog="camflow",
        description="cam-flow: lightweight stateful workflow engine for agent execution",
    )
    parser.add_argument("workflow", help="Path to workflow YAML file")
    parser.add_argument(
        "--project-dir", "-p",
        default=None,
        help="Project directory (default: directory of the workflow file)",
    )
    parser.add_argument("--validate", action="store_true", help="Validate workflow and exit")
    parser.add_argument("--dry-run", action="store_true", help="Static walk without executing nodes")
    parser.add_argument(
        "--force-restart", action="store_true",
        help="Discard any current_agent_id on resume instead of adopting the orphan",
    )
    parser.add_argument("--poll-interval", type=int, default=5, help="camc status poll interval (seconds)")
    parser.add_argument("--node-timeout", type=int, default=600, help="per-node timeout (seconds)")
    parser.add_argument("--workflow-timeout", type=int, default=3600, help="overall timeout (seconds)")
    parser.add_argument("--max-retries", type=int, default=3, help="per-node retry budget")
    parser.add_argument(
        "--max-node-executions", type=int, default=10,
        help="loop-detection cap: max times any single node can execute in one run",
    )
    args = parser.parse_args()

    # Load + validate
    workflow = load_workflow(args.workflow)
    valid, errors = validate_workflow(workflow)
    if not valid:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)

    if args.validate:
        print("Workflow is valid.")
        sys.exit(0)

    # Resolve project dir (defaults to the workflow's directory)
    project_dir = args.project_dir or os.path.dirname(os.path.abspath(args.workflow)) or "."

    cfg = EngineConfig(
        poll_interval=args.poll_interval,
        node_timeout=args.node_timeout,
        workflow_timeout=args.workflow_timeout,
        max_retries=args.max_retries,
        max_node_executions=args.max_node_executions,
        dry_run=args.dry_run,
        force_restart=args.force_restart,
    )

    engine = Engine(args.workflow, project_dir, cfg)
    result = engine.run()

    if args.dry_run:
        # engine.run returns an int (exit code) in dry-run mode
        sys.exit(result if isinstance(result, int) else 0)

    status = result.get("status") if isinstance(result, dict) else None
    print(f"Workflow finished: status={status}, pc={result.get('pc') if isinstance(result, dict) else None}")
    sys.exit(0 if status == "done" else 1)


if __name__ == "__main__":
    main()
