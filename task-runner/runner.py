"""
Task Runner — creates a webtop pod, runs the GPT-5.4 computer-use agent loop
against it via k8s exec, and reports results back to the orchestrator.
"""

import base64
import os
import sys
import time
from pathlib import Path

import requests
from kubernetes import client, config
from kubernetes.stream import stream
from openai import OpenAI

# ---------------------------------------------------------------------------
# Configuration (injected via env by the orchestrator)
# ---------------------------------------------------------------------------

TASK_ID = os.environ["TASK_ID"]
TASK_MESSAGE = os.environ["TASK_MESSAGE"]
ORCHESTRATOR_URL = os.environ["ORCHESTRATOR_URL"]
WEBTOP_IMAGE = os.environ.get("WEBTOP_IMAGE", "webtop:latest")
NAMESPACE = os.environ.get("POD_NAMESPACE", "default")
MAX_STEPS = int(os.environ.get("MAX_STEPS", "30"))

DISPLAY = ":1"
WEBTOP_POD = f"webtop-{TASK_ID[:8]}"
SCREENSHOTS_DIR = Path("/screenshots") / TASK_ID

SYSTEM_PROMPT = """\
You are a desktop operator. You complete tasks by controlling a Linux desktop \
through screenshots, mouse, and keyboard actions.

# Environment

- Debian Linux, KDE Plasma desktop, 1024x768 resolution.
- Display: :1. You are logged in as root.
- Browser: Chromium (command: `chromium`).
- Terminal: Konsole.
- File manager: Dolphin.
- Text editor: Kate / KWrite.

# How to open applications

Preferred method: press Alt+F2 to open KRunner (the KDE run dialog). \
Type the app name (e.g. "chromium", "konsole", "dolphin") and press Enter. \
This is the fastest and most reliable way to launch any application.

Alternative: click the application launcher icon in the bottom-left corner \
of the KDE panel (taskbar at the bottom of the screen). \
Browse or search for applications from there.
- Right-clicking the desktop does NOT open an app launcher in this setup.

# Interaction guidelines

- Always start by taking a screenshot to see the current desktop state.
- After every meaningful action, verify the result in the next screenshot \
  before moving on.
- If an action did not produce the expected result (e.g. a window did not \
  open), try an alternative approach — do not repeat the same failing action.
- When typing into a browser address bar, click the bar first to focus it, \
  then type the URL/query, then press Enter.
- When using Chromium for the first time, it may show a welcome dialog or \
  set-as-default prompt. Dismiss or close any such dialog before proceeding.
- Wait after launching applications — they may take 1-2 seconds to appear.
- Prefer clicking UI elements you can see in the screenshot over keyboard \
  shortcuts, since not all shortcuts are configured in this environment.

# Task completion

- Only report the task as done when you have visually confirmed the result \
  in a screenshot.
- If the task asks you to search for something, confirm you can see the \
  search results on screen before finishing.
"""

# ---------------------------------------------------------------------------
# K8s setup
# ---------------------------------------------------------------------------

config.load_incluster_config()
k8s = client.CoreV1Api()

# ---------------------------------------------------------------------------
# Key mapping (GPT-5.4 names -> xdotool names)
# ---------------------------------------------------------------------------

KEY_MAP = {
    "ENTER": "Return", "RETURN": "Return", "TAB": "Tab",
    "ESCAPE": "Escape", "ESC": "Escape", "SPACE": "space",
    "BACKSPACE": "BackSpace", "DELETE": "Delete",
    "UP": "Up", "DOWN": "Down", "LEFT": "Left", "RIGHT": "Right",
    "HOME": "Home", "END": "End",
    "PAGEUP": "Page_Up", "PAGEDOWN": "Page_Down",
    "CTRL": "ctrl", "ALT": "alt", "SHIFT": "shift",
    "SUPER": "super", "META": "super",
}
BUTTON_MAP = {"left": 1, "middle": 2, "right": 3}


def normalize_key(key: str) -> str:
    return KEY_MAP.get(key.upper(), KEY_MAP.get(key, key))


# ---------------------------------------------------------------------------
# Webtop pod lifecycle
# ---------------------------------------------------------------------------


def create_webtop_pod():
    pod = client.V1Pod(
        metadata=client.V1ObjectMeta(
            name=WEBTOP_POD,
            namespace=NAMESPACE,
            labels={"app": "webtop", "task-id": TASK_ID},
        ),
        spec=client.V1PodSpec(
            restart_policy="Never",
            containers=[
                client.V1Container(
                    name="webtop",
                    image=WEBTOP_IMAGE,
                    image_pull_policy="Never",
                    env=[
                        client.V1EnvVar(name="PUID", value="1000"),
                        client.V1EnvVar(name="PGID", value="1000"),
                        client.V1EnvVar(name="TZ", value="Etc/UTC"),
                        # View-only: disable interactive features
                        client.V1EnvVar(
                            name="SELKIES_ENABLE_VIEW_ONLY_LINK", value="true"
                        ),
                        client.V1EnvVar(
                            name="SELKIES_CLIPBOARD_ENABLED", value="false"
                        ),
                        client.V1EnvVar(
                            name="SELKIES_FILE_TRANSFERS", value="none"
                        ),
                        client.V1EnvVar(
                            name="SELKIES_GAMEPAD_ENABLED", value="false"
                        ),
                    ],
                    ports=[
                        client.V1ContainerPort(container_port=3000, name="vnc"),
                    ],
                    security_context=client.V1SecurityContext(
                        capabilities=client.V1Capabilities(add=["SYS_ADMIN"]),
                    ),
                    resources=client.V1ResourceRequirements(
                        requests={"memory": "512Mi", "cpu": "500m"},
                        limits={"memory": "2Gi", "cpu": "2"},
                    ),
                ),
            ],
        ),
    )
    k8s.create_namespaced_pod(namespace=NAMESPACE, body=pod)
    print(f"[runner] Created webtop pod: {WEBTOP_POD}")


def wait_for_webtop(timeout: int = 180):
    """Block until the webtop X display is responsive."""
    start = time.time()

    # Phase 1: wait for pod Running
    while time.time() - start < timeout:
        pod = k8s.read_namespaced_pod(WEBTOP_POD, NAMESPACE)
        if pod.status.phase == "Running":
            break
        print(f"[runner] Webtop pod phase: {pod.status.phase}")
        time.sleep(5)
    else:
        raise TimeoutError("Webtop pod never reached Running")

    # Phase 2: wait for X display
    print("[runner] Waiting for X display ...")
    while time.time() - start < timeout:
        try:
            out = _exec(f"DISPLAY={DISPLAY} xdotool getdisplaygeometry")
            if out.strip():
                print(f"[runner] Display ready: {out.strip()}")
                return
        except Exception:
            pass
        time.sleep(5)

    raise TimeoutError("Webtop X display never became ready")


# ---------------------------------------------------------------------------
# Exec helper
# ---------------------------------------------------------------------------


def _exec(cmd: str) -> str:
    """Run a shell command inside the webtop container via k8s exec."""
    return stream(
        k8s.connect_get_namespaced_pod_exec,
        WEBTOP_POD,
        NAMESPACE,
        container="webtop",
        command=["sh", "-c", cmd],
        stdout=True,
        stderr=True,
        stdin=False,
        tty=False,
    )


# ---------------------------------------------------------------------------
# Screenshot
# ---------------------------------------------------------------------------


def capture_screenshot(step: int) -> bytes:
    """Take a screenshot, save to disk, return raw PNG bytes."""
    b64_str = _exec(
        f"DISPLAY={DISPLAY} import -window root png:- | base64 -w0"
    )
    png_bytes = base64.b64decode(b64_str.strip())

    # Save to shared hostPath volume -> appears on host filesystem
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOTS_DIR / f"step_{step:02d}.png"
    path.write_bytes(png_bytes)
    print(f"    screenshot saved: {path} ({len(png_bytes):,} bytes)")

    return png_bytes


# ---------------------------------------------------------------------------
# Action execution
# ---------------------------------------------------------------------------


def handle_actions(actions):
    for action in actions:
        atype = action.type
        match atype:
            case "click":
                btn = BUTTON_MAP.get(getattr(action, "button", "left"), 1)
                print(f"    click ({action.x}, {action.y}) button={btn}")
                _exec(
                    f"DISPLAY={DISPLAY} xdotool mousemove {action.x} {action.y} click {btn}"
                )
            case "double_click":
                btn = BUTTON_MAP.get(getattr(action, "button", "left"), 1)
                print(f"    double_click ({action.x}, {action.y})")
                _exec(
                    f"DISPLAY={DISPLAY} xdotool mousemove {action.x} {action.y} "
                    f"click --repeat 2 {btn}"
                )
            case "scroll":
                scroll_y = getattr(action, "scrollY", 0)
                btn = 4 if scroll_y < 0 else 5
                clicks = max(1, abs(round(scroll_y / 100)))
                print(f"    scroll ({action.x}, {action.y}) scrollY={scroll_y}")
                _exec(f"DISPLAY={DISPLAY} xdotool mousemove {action.x} {action.y}")
                for _ in range(clicks):
                    _exec(f"DISPLAY={DISPLAY} xdotool click {btn}")
            case "keypress":
                raw = action.keys
                combo = "+".join(normalize_key(k) for k in raw)
                print(f"    keypress {raw} -> {combo}")
                _exec(f"DISPLAY={DISPLAY} xdotool key '{combo}'")
            case "type":
                text = action.text.replace("'", "'\\''")
                print(f"    type {action.text!r}")
                _exec(f"DISPLAY={DISPLAY} xdotool type --delay 12 '{text}'")
            case "drag":
                path = action.path
                print(f"    drag path={path}")
                if len(path) >= 2:
                    s, e = path[0], path[-1]
                    _exec(
                        f"DISPLAY={DISPLAY} xdotool mousemove {s['x']} {s['y']} "
                        f"mousedown 1 mousemove {e['x']} {e['y']} mouseup 1"
                    )
            case "move":
                print(f"    move ({action.x}, {action.y})")
                _exec(f"DISPLAY={DISPLAY} xdotool mousemove {action.x} {action.y}")
            case "wait":
                print("    wait 2s")
                time.sleep(2)
            case "screenshot":
                pass  # captured after all actions
            case _:
                print(f"    [warn] unsupported action: {atype}")


# ---------------------------------------------------------------------------
# Orchestrator callback
# ---------------------------------------------------------------------------


def _callback(status: str, result: str | None = None, steps: list | None = None):
    try:
        requests.post(
            f"{ORCHESTRATOR_URL}/tasks/{TASK_ID}/callback",
            json={"status": status, "result": result, "steps": steps},
            timeout=10,
        )
    except Exception as e:
        print(f"[runner] callback failed: {e}")


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------


def log_output(response):
    for item in response.output:
        if item.type == "message":
            for c in item.content:
                if hasattr(c, "text"):
                    print(f"    [message] {c.text}")
        elif item.type == "reasoning":
            for c in getattr(item, "summary", []):
                if hasattr(c, "text"):
                    print(f"    [reasoning] {c.text}")


def run_agent():
    ai = OpenAI()
    steps_log: list[dict] = []

    print(f"\n{'='*60}")
    print(f"[agent] Task: {TASK_MESSAGE}")
    print(f"[agent] Max steps: {MAX_STEPS}")
    print(f"[agent] Screenshots: {SCREENSHOTS_DIR}")
    print(f"{'='*60}\n")

    t0 = time.time()
    response = ai.responses.create(
        model="gpt-5.4",
        instructions=SYSTEM_PROMPT,
        tools=[{"type": "computer"}],
        input=TASK_MESSAGE,
    )
    print(f"[agent] Initial response in {time.time()-t0:.1f}s (id={response.id})")
    log_output(response)

    for step in range(MAX_STEPS):
        computer_call = next(
            (i for i in response.output if i.type == "computer_call"), None
        )
        if computer_call is None:
            final = ""
            for item in response.output:
                if hasattr(item, "content"):
                    for c in item.content:
                        if hasattr(c, "text"):
                            final += c.text + "\n"
            print(f"\n[agent] Finished after {step} steps")
            _callback("completed", result=final.strip() or None, steps=steps_log)
            return

        actions = computer_call.actions
        action_types = [a.type for a in actions]
        print(f"\n[step {step+1}/{MAX_STEPS}] call_id={computer_call.call_id}")
        print(f"    actions: {action_types}")

        handle_actions(actions)
        time.sleep(1)

        screenshot_bytes = capture_screenshot(step + 1)
        screenshot_b64 = base64.b64encode(screenshot_bytes).decode()

        steps_log.append({"step": step + 1, "actions": action_types})

        t0 = time.time()
        response = ai.responses.create(
            model="gpt-5.4",
            instructions=SYSTEM_PROMPT,
            tools=[{"type": "computer"}],
            previous_response_id=response.id,
            input=[
                {
                    "type": "computer_call_output",
                    "call_id": computer_call.call_id,
                    "output": {
                        "type": "computer_screenshot",
                        "image_url": f"data:image/png;base64,{screenshot_b64}",
                    },
                }
            ],
        )
        print(f"    model responded in {time.time()-t0:.1f}s (id={response.id})")
        log_output(response)

    print(f"\n[agent] Reached max steps ({MAX_STEPS})")
    _callback("completed", result="Reached max steps", steps=steps_log)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"[runner] task_id={TASK_ID}")
    print(f"[runner] message={TASK_MESSAGE}")
    print(f"[runner] webtop_pod={WEBTOP_POD}")
    print(f"[runner] namespace={NAMESPACE}")

    _callback("running")

    try:
        create_webtop_pod()
        wait_for_webtop()
        run_agent()
    except Exception as e:
        print(f"[runner] FAILED: {e}")
        _callback("failed", result=str(e))
        sys.exit(1)
