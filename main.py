"""
CLI client for the Task Orchestrator.

Usage:
    python main.py --message "open chrome and google weather in SF"
"""

import argparse
import time

import requests

DEFAULT_URL = "http://localhost:8000"


def main():
    parser = argparse.ArgumentParser(description="CUA Task Client")
    parser.add_argument("--message", "-m", required=True, help="Task instruction")
    parser.add_argument("--url", default=DEFAULT_URL, help="Orchestrator URL")
    parser.add_argument(
        "--max-steps", type=int, default=30, help="Max agent steps"
    )
    parser.add_argument(
        "--poll", type=int, default=5, help="Status poll interval (seconds)"
    )
    args = parser.parse_args()

    # Submit task
    print(f"[client] Submitting task: {args.message}")
    resp = requests.post(
        f"{args.url}/tasks",
        json={"message": args.message, "max_steps": args.max_steps},
    )
    resp.raise_for_status()
    task = resp.json()

    task_id = task["id"]
    short_id = task_id[:8]
    vnc_url = task.get("vnc_url", "N/A")
    print(f"[client] Task created: {task_id}")
    print(f"[client] Runner pod:   {task['runner_pod']}")
    print(f"[client] Webtop pod:   {task['webtop_pod']}")
    print(f"[client] VNC desktop:  {vnc_url}")
    print()
    print(f"  Runner logs:  task logs-runner ID={short_id}")
    print(f"  Webtop logs:  task logs-webtop ID={short_id}")
    print()

    # Poll for completion
    prev_status = None
    try:
        while True:
            time.sleep(args.poll)
            resp = requests.get(f"{args.url}/tasks/{task_id}")
            info = resp.json()
            status = info["status"]
            if status != prev_status:
                print(f"[client] Status: {status}")
                prev_status = status
            if status in ("completed", "failed"):
                if info.get("result"):
                    print(f"[client] Result: {info['result']}")
                if info.get("steps"):
                    print(f"[client] Total steps: {len(info['steps'])}")
                break
    except KeyboardInterrupt:
        print("\n[client] Interrupted (task keeps running in cluster)")

    print("[client] Done.")


if __name__ == "__main__":
    main()
