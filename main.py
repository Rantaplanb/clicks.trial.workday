"""
GPT-5.4 Computer Use Agent — runs tasks in a Linux desktop container.

Usage:
    python main.py --message "open chrome and google weather in SF"
"""
import argparse
import base64
import subprocess
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

CONTAINER_NAME = "cua-webtop"
IMAGE = "lscr.io/linuxserver/webtop:debian-kde"
DISPLAY = ":1"
VNC_PORT = 6080  # view-only noVNC web viewer
MAX_STEPS = 30

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
# Container helpers
# ---------------------------------------------------------------------------

def docker_exec(cmd: str, decode: bool = True):
    safe_cmd = cmd.replace('"', '\\"')
    docker_cmd = f'docker exec {CONTAINER_NAME} sh -c "{safe_cmd}"'
    output = subprocess.check_output(docker_cmd, shell=True)
    if decode:
        return output.decode("utf-8", errors="ignore")
    return output


def start_container():
    """Start the webtop container and install required tools."""
    # Remove stale container if any
    subprocess.run(
        f"docker rm -f {CONTAINER_NAME}",
        shell=True, capture_output=True, check=False,
    )

    print(f"[container] Starting {IMAGE} ...")
    subprocess.run(
        f"docker run -d --name {CONTAINER_NAME} "
        f"-p {VNC_PORT}:6080 "
        f"-e PUID=1000 -e PGID=1000 -e TZ=Etc/UTC "
        f"--shm-size=1g "
        f"{IMAGE}",
        shell=True, check=True, capture_output=True,
    )

    print("[container] Waiting for desktop to initialize ...")
    time.sleep(10)

    # Install imagemagick (screenshots), x11vnc (view-only VNC), novnc (web viewer)
    print("[container] Installing imagemagick, x11vnc, novnc ...")
    subprocess.run(
        f"docker exec {CONTAINER_NAME} bash -c "
        f"'apt-get update -qq && apt-get install -y -qq imagemagick x11vnc novnc > /dev/null 2>&1'",
        shell=True, check=True, capture_output=True,
    )

    # Start x11vnc in view-only mode (no mouse/keyboard input from viewers)
    # -noshm required inside Docker (MIT-SHM not available)
    print("[container] Starting view-only VNC ...")
    subprocess.run(
        f"docker exec -d {CONTAINER_NAME} "
        f"x11vnc -display {DISPLAY} -viewonly -shared -forever -nopw -noshm -rfbport 5900",
        shell=True, check=True, capture_output=True,
    )
    time.sleep(2)

    # Start noVNC web proxy (serves a browser-based viewer on port 6080)
    subprocess.run(
        f"docker exec -d {CONTAINER_NAME} bash -c "
        f"'/usr/share/novnc/utils/novnc_proxy --vnc localhost:5900 --listen 6080 > /dev/null 2>&1'",
        shell=True, check=True, capture_output=True,
    )
    time.sleep(1)

    print("[container] Ready!")
    print(f"[container] View-only VNC: http://localhost:{VNC_PORT}/vnc.html")


def stop_container():
    print("\n[container] Stopping ...")
    subprocess.run(f"docker stop {CONTAINER_NAME}", shell=True, capture_output=True)
    subprocess.run(f"docker rm {CONTAINER_NAME}", shell=True, capture_output=True)
    print("[container] Removed.")


# ---------------------------------------------------------------------------
# Screenshot
# ---------------------------------------------------------------------------

def capture_screenshot() -> bytes:
    return docker_exec(
        f"export DISPLAY={DISPLAY} && import -window root png:-",
        decode=False,
    )


# ---------------------------------------------------------------------------
# Action execution
# ---------------------------------------------------------------------------

BUTTON_MAP = {"left": 1, "middle": 2, "right": 3}

# GPT-5.4 key names → xdotool key names
KEY_MAP = {
    "ENTER": "Return",
    "RETURN": "Return",
    "TAB": "Tab",
    "ESCAPE": "Escape",
    "ESC": "Escape",
    "SPACE": "space",
    "BACKSPACE": "BackSpace",
    "DELETE": "Delete",
    "UP": "Up",
    "DOWN": "Down",
    "LEFT": "Left",
    "RIGHT": "Right",
    "HOME": "Home",
    "END": "End",
    "PAGEUP": "Page_Up",
    "PAGEDOWN": "Page_Down",
    "CTRL": "ctrl",
    "ALT": "alt",
    "SHIFT": "shift",
    "SUPER": "super",
    "META": "super",
}


def normalize_key(key: str) -> str:
    return KEY_MAP.get(key.upper(), KEY_MAP.get(key, key))


def handle_actions(actions):
    """Execute a batch of computer-use actions via xdotool."""
    for action in actions:
        atype = action.type
        match atype:
            case "click":
                button = BUTTON_MAP.get(getattr(action, "button", "left"), 1)
                print(f"         click ({action.x}, {action.y}) button={button}")
                docker_exec(
                    f"DISPLAY={DISPLAY} xdotool mousemove {action.x} {action.y} click {button}"
                )
            case "double_click":
                button = BUTTON_MAP.get(getattr(action, "button", "left"), 1)
                print(f"         double_click ({action.x}, {action.y})")
                docker_exec(
                    f"DISPLAY={DISPLAY} xdotool mousemove {action.x} {action.y} click --repeat 2 {button}"
                )
            case "scroll":
                scroll_y = getattr(action, "scrollY", 0)
                btn = 4 if scroll_y < 0 else 5
                clicks = max(1, abs(round(scroll_y / 100)))
                print(f"         scroll ({action.x}, {action.y}) scrollY={scroll_y}")
                docker_exec(
                    f"DISPLAY={DISPLAY} xdotool mousemove {action.x} {action.y}"
                )
                for _ in range(clicks):
                    docker_exec(f"DISPLAY={DISPLAY} xdotool click {btn}")
            case "keypress":
                raw_keys = action.keys
                normalized = [normalize_key(k) for k in raw_keys]
                # xdotool expects combos as "ctrl+alt+t", single keys as "Return"
                combo = "+".join(normalized)
                print(f"         keypress {raw_keys} -> {combo}")
                docker_exec(
                    f"DISPLAY={DISPLAY} xdotool key '{combo}'"
                )
            case "type":
                print(f"         type {action.text!r}")
                text = action.text.replace("'", "'\\''")
                docker_exec(
                    f"DISPLAY={DISPLAY} xdotool type --delay 12 '{text}'"
                )
            case "drag":
                path = action.path
                print(f"         drag path={path}")
                if len(path) >= 2:
                    start = path[0]
                    end = path[-1]
                    docker_exec(
                        f"DISPLAY={DISPLAY} xdotool mousemove {start['x']} {start['y']} "
                        f"mousedown 1 mousemove {end['x']} {end['y']} mouseup 1"
                    )
            case "move":
                print(f"         move ({action.x}, {action.y})")
                docker_exec(
                    f"DISPLAY={DISPLAY} xdotool mousemove {action.x} {action.y}"
                )
            case "wait":
                print(f"         wait 2s")
                time.sleep(2)
            case "screenshot":
                print(f"         screenshot (captured after all actions)")
            case _:
                print(f"         [warn] Unsupported action: {atype}")


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def log_response_output(response):
    """Log all output items from a response (reasoning, messages, etc.)."""
    for item in response.output:
        if item.type == "message":
            for content in item.content:
                if hasattr(content, "text"):
                    print(f"         [model message] {content.text}")
        elif item.type == "reasoning":
            for content in getattr(item, "summary", []):
                if hasattr(content, "text"):
                    print(f"         [reasoning] {content.text}")
        elif item.type == "computer_call":
            pass  # handled separately
        else:
            print(f"         [output] type={item.type}")


def run_agent(message: str, screenshots_dir: Path):
    client = OpenAI()
    run_start = time.time()

    print(f"\n{'='*60}")
    print(f"[agent] Task: {message}")
    print(f"[agent] Screenshots → {screenshots_dir}/")
    print(f"[agent] Max steps: {MAX_STEPS}")
    print(f"{'='*60}")

    print("\n[agent] Sending initial request to GPT-5.4 ...")
    t0 = time.time()
    response = client.responses.create(
        model="gpt-5.4",
        instructions=SYSTEM_PROMPT,
        tools=[{"type": "computer"}],
        input=message,
    )
    print(f"[agent] Response received in {time.time() - t0:.1f}s  (id={response.id})")
    log_response_output(response)

    for step in range(MAX_STEPS):
        # Find computer_call in output
        computer_call = next(
            (item for item in response.output if item.type == "computer_call"),
            None,
        )

        if computer_call is None:
            # No more actions — print final output
            elapsed = time.time() - run_start
            print(f"\n{'='*60}")
            print(f"[agent] Done after {step} steps in {elapsed:.1f}s")
            for item in response.output:
                if hasattr(item, "content"):
                    for content in item.content:
                        if hasattr(content, "text"):
                            print(f"[agent] Model says: {content.text}")
            print(f"{'='*60}")
            return

        actions = computer_call.actions
        action_types = [a.type for a in actions]
        print(f"\n[step {step + 1}/{MAX_STEPS}] call_id={computer_call.call_id}")
        print(f"         actions: {action_types}")

        # Execute actions
        handle_actions(actions)

        # Small pause for UI to settle
        time.sleep(1)

        # Capture screenshot
        screenshot_bytes = capture_screenshot()
        screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

        # Save screenshot locally
        ss_path = screenshots_dir / f"step_{step + 1:02d}.png"
        ss_path.write_bytes(screenshot_bytes)
        print(f"         screenshot: {ss_path} ({len(screenshot_bytes):,} bytes)")

        # Send screenshot back
        t0 = time.time()
        response = client.responses.create(
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
                        "detail": "original",
                    },
                }
            ],
        )
        print(f"         model responded in {time.time() - t0:.1f}s  (id={response.id})")
        log_response_output(response)

    elapsed = time.time() - run_start
    print(f"\n[agent] Reached max steps ({MAX_STEPS}) after {elapsed:.1f}s.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="GPT-5.4 Computer Use Agent")
    parser.add_argument("--message", "-m", required=True, help="Task instruction")
    parser.add_argument("--keep", action="store_true", help="Keep the container running after the task")
    args = parser.parse_args()

    # Create a timestamped screenshots directory per run
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshots_dir = Path("screenshots") / run_id
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    try:
        start_container()
        run_agent(args.message, screenshots_dir)
    except KeyboardInterrupt:
        print("\n[agent] Interrupted.")
    finally:
        if not args.keep:
            stop_container()
        else:
            print(f"\n[container] Left running. View-only VNC: http://localhost:{VNC_PORT}/vnc.html")
            print(f"[container] Stop with: docker rm -f {CONTAINER_NAME}")


if __name__ == "__main__":
    main()
