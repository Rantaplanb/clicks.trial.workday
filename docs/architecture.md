# CUA Task Orchestrator — Architecture

## High-Level Architecture

```mermaid
graph TB
    subgraph macOS["macOS Host"]
        CLI["main.py<br/>(CLI Client)"]
        Browser["Browser"]
        SS_DIR["./screenshots/"]
    end

    subgraph Kind["Kind Cluster (Docker)"]
        subgraph Orchestrator["Orchestrator Pod"]
            API["FastAPI<br/>:8000"]
            Dashboard["Web Dashboard<br/>(SPA)"]
            TaskJSON[("Task JSON<br/>/data/tasks/")]
        end

        subgraph Runner["Runner Pod<br/>(per task)"]
            AgentLoop["GPT-5.4<br/>Agent Loop"]
        end

        subgraph Webtop["Webtop Pod<br/>(per task)"]
            KDE["KDE Plasma<br/>Desktop"]
            X11["X11 Display :1"]
            xdotool["xdotool"]
            x11vnc["x11vnc<br/>(view-only)"]
            noVNC["noVNC<br/>:6080"]
        end

        K8sAPI["Kubernetes API"]
        VNCsvc["VNC NodePort<br/>Service"]
        OrcSvc["Orchestrator<br/>NodePort :30080"]
        SSvol[("/data/screenshots<br/>(hostPath)")]
    end

    CLI -->|"POST /tasks"| API
    Browser -->|":8000"| Dashboard
    Browser -->|":6901-6910"| noVNC

    API -->|"create pods/services"| K8sAPI
    AgentLoop -->|"k8s exec<br/>xdotool + screenshot"| Webtop
    AgentLoop -->|"POST /callback"| API
    AgentLoop -->|"save PNGs"| SSvol
    API -->|"read PNGs"| SSvol
    SSvol ---|"kind extraMount"| SS_DIR

    OrcSvc --> API
    VNCsvc --> noVNC
    x11vnc --> X11
    noVNC --> x11vnc
    xdotool --> X11
```

## Component Overview

| Component | Image | Role |
|---|---|---|
| **Orchestrator** | `python:3.12-slim` | REST API, dashboard, task lifecycle, k8s resource management |
| **Task Runner** | `python:3.12-slim` | Creates webtop pod, runs GPT-5.4 agent loop, captures screenshots |
| **Webtop** | `linuxserver/webtop:debian-kde` | KDE desktop with Chromium, xdotool, x11vnc, noVNC |

## Happy Path — Task Execution

```mermaid
sequenceDiagram
    actor User
    participant CLI as CLI / Dashboard
    participant Orch as Orchestrator
    participant K8s as Kubernetes API
    participant Runner as Task Runner Pod
    participant Webtop as Webtop Pod
    participant GPT as GPT-5.4

    User->>CLI: Submit task<br/>"open chromium, search weather in SF"
    CLI->>Orch: POST /tasks {message, max_steps}
    activate Orch

    Orch->>Orch: Allocate VNC port (30001-30010)
    Orch->>K8s: Create runner pod
    Orch->>K8s: Create VNC NodePort service
    Orch->>Orch: Save task JSON (status: pending)
    Orch-->>CLI: 200 {task_id, vnc_url}
    deactivate Orch

    activate Runner
    Runner->>Orch: POST /callback (status: running)
    Runner->>K8s: Create webtop pod
    K8s-->>Runner: Pod created

    loop Wait for desktop
        Runner->>Webtop: k8s exec: xdotool getdisplaygeometry
    end
    Webtop-->>Runner: "1024 768"

    Runner->>Webtop: k8s exec: x11vnc -viewonly -shared ...
    Runner->>Webtop: k8s exec: novnc_proxy --vnc localhost:5900

    Note over User,Webtop: User can now watch via VNC (view-only)

    Runner->>GPT: responses.create(model="gpt-5.4",<br/>tools=[computer], input=task_message)
    activate GPT
    GPT-->>Runner: computer_call [click, type, ...]
    deactivate GPT

    loop Agent loop (up to max_steps)
        Runner->>Webtop: k8s exec: xdotool mousemove/click/type
        Runner->>Webtop: k8s exec: import -window root png:- | base64
        Webtop-->>Runner: screenshot (base64 PNG)
        Runner->>Runner: Save screenshot to /screenshots/{task_id}/

        Runner->>GPT: responses.create(computer_call_output<br/>+ screenshot, previous_response_id)
        activate GPT
        alt More actions needed
            GPT-->>Runner: computer_call [next actions...]
        else Task complete
            GPT-->>Runner: message "Done — results visible"
        end
        deactivate GPT
    end

    Runner->>Orch: POST /callback (status: completed, result)
    deactivate Runner

    User->>CLI: View result
    CLI->>Orch: GET /tasks/{id}
    Orch-->>CLI: {status: completed, result, steps}
```

## Networking

```mermaid
graph LR
    subgraph Host["macOS"]
        P8000[":8000"]
        P6901[":6901"]
        P6902[":6902"]
        Pdots["..."]
        P6910[":6910"]
    end

    subgraph Kind["Kind Node"]
        NP30080["NodePort :30080"]
        NP30001["NodePort :30001"]
        NP30002["NodePort :30002"]
        NPdots["..."]
        NP30010["NodePort :30010"]
    end

    subgraph Pods
        O["Orchestrator :8000"]
        W1["Webtop-1 :6080"]
        W2["Webtop-2 :6080"]
        Wdots["..."]
        W10["Webtop-10 :6080"]
    end

    P8000 --> NP30080 --> O
    P6901 --> NP30001 --> W1
    P6902 --> NP30002 --> W2
    Pdots --> NPdots --> Wdots
    P6910 --> NP30010 --> W10
```

Port mapping chain: **macOS port** → kind `extraPortMappings` → **NodePort service** → **pod container port**

## Screenshot Persistence

```mermaid
graph LR
    Runner["Task Runner Pod<br/>/screenshots/{task_id}/"] -->|"writes PNGs"| NodeFS[("Kind Node<br/>/data/screenshots/")]
    Orch["Orchestrator Pod<br/>/data/screenshots/"] -->|"serves via API"| NodeFS
    NodeFS -->|"kind extraMount"| Host["macOS Host<br/>./screenshots/"]
```

Screenshots flow through a shared `hostPath` volume mounted into both the runner (write) and orchestrator (read/serve) pods, with kind's `extraMounts` syncing the node filesystem back to the macOS host.

## RBAC

```mermaid
graph TD
    subgraph SA["Service Accounts"]
        OSA["orchestrator-sa"]
        TSA["task-runner-sa"]
    end

    subgraph Perms["Permissions"]
        OSA -->|"pods"| CRUD1["create, get, list, delete, watch"]
        OSA -->|"services"| CRUD2["create, get, list, delete"]
        OSA -->|"pods/log"| R1["get"]

        TSA -->|"pods"| CGD["create, get, delete"]
        TSA -->|"pods/exec"| CG["create, get"]
    end
```

The task-runner needs `get` on `pods/exec` because the Kubernetes Python client uses a websocket-based GET for `stream()`.

## Task Lifecycle

```mermaid
stateDiagram-v2
    [*] --> pending: POST /tasks
    pending --> running: Runner calls back
    running --> completed: Agent finishes or max_steps reached
    running --> failed: Exception in runner
    completed --> [*]: DELETE /tasks/{id}
    failed --> [*]: DELETE /tasks/{id}
```

## Project Structure

```
clicks.trial.workday/
├── orchestrator/
│   ├── app.py                 # FastAPI orchestrator
│   ├── Dockerfile
│   ├── requirements.txt
│   └── static/index.html      # Web dashboard (SPA)
├── task-runner/
│   ├── runner.py              # Agent loop + webtop lifecycle
│   ├── Dockerfile
│   └── requirements.txt
├── webtop/
│   └── Dockerfile             # KDE desktop + chromium + xdotool + vnc
├── k8s/
│   ├── orchestrator.yaml      # Deployment + NodePort Service
│   └── rbac.yaml              # ServiceAccounts, Roles, RoleBindings
├── kind-config.yaml           # Cluster config (ports + mounts)
├── Taskfile.yml               # bootstrap-cluster, build, deploy, etc.
├── main.py                    # CLI client
└── docs/
    └── architecture.md        # This file
```
