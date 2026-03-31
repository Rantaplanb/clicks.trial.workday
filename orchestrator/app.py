"""Task Orchestrator — manages computer-use agent tasks on Kubernetes."""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from kubernetes import client, config
from pydantic import BaseModel

app = FastAPI(title="CUA Task Orchestrator")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TASKS_DIR = Path(os.getenv("TASKS_DIR", "/data/tasks"))
TASKS_DIR.mkdir(parents=True, exist_ok=True)

NAMESPACE = os.getenv("NAMESPACE", "default")
TASK_RUNNER_IMAGE = os.getenv("TASK_RUNNER_IMAGE", "task-runner:latest")
WEBTOP_IMAGE = os.getenv("WEBTOP_IMAGE", "webtop:latest")
ORCHESTRATOR_URL = os.getenv(
    "ORCHESTRATOR_URL", "http://orchestrator.default.svc.cluster.local:8000"
)

# VNC NodePort pool: 30001-30010 mapped to host 6901-6910 via kind-config
VNC_PORT_MIN = 30001
VNC_PORT_MAX = 30010

# K8s client
try:
    config.load_incluster_config()
except config.ConfigException:
    config.load_kube_config()

k8s = client.CoreV1Api()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TaskCreate(BaseModel):
    message: str
    max_steps: int = 30


class TaskCallback(BaseModel):
    status: str  # running, completed, failed
    result: str | None = None
    steps: list | None = None


class Task(BaseModel):
    id: str
    message: str
    status: str
    created_at: str
    updated_at: str
    runner_pod: str | None = None
    webtop_pod: str | None = None
    vnc_url: str | None = None
    result: str | None = None
    steps: list = []


# ---------------------------------------------------------------------------
# Persistence (filesystem JSON)
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _save(task: dict):
    (TASKS_DIR / f"{task['id']}.json").write_text(json.dumps(task, indent=2))


def _load(task_id: str) -> dict:
    path = TASKS_DIR / f"{task_id}.json"
    if not path.exists():
        raise HTTPException(404, "Task not found")
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# VNC port allocation
# ---------------------------------------------------------------------------


def _used_vnc_ports() -> set[int]:
    """Return the set of VNC NodePorts currently allocated to tasks."""
    ports = set()
    for f in TASKS_DIR.glob("*.json"):
        task = json.loads(f.read_text())
        if task.get("vnc_port"):
            ports.add(task["vnc_port"])
    return ports


def _allocate_vnc_port() -> int:
    """Find the next free VNC NodePort."""
    used = _used_vnc_ports()
    for port in range(VNC_PORT_MIN, VNC_PORT_MAX + 1):
        if port not in used:
            return port
    raise HTTPException(503, "No VNC ports available — too many concurrent tasks")


# ---------------------------------------------------------------------------
# K8s helpers
# ---------------------------------------------------------------------------


def _create_runner_pod(task_id: str, message: str, max_steps: int) -> str:
    """Spin up a task-runner pod that will create its own webtop and run the agent."""
    pod_name = f"runner-{task_id[:8]}"

    pod = client.V1Pod(
        metadata=client.V1ObjectMeta(
            name=pod_name,
            namespace=NAMESPACE,
            labels={"app": "task-runner", "task-id": task_id},
        ),
        spec=client.V1PodSpec(
            service_account_name="task-runner-sa",
            restart_policy="Never",
            containers=[
                client.V1Container(
                    name="runner",
                    image=TASK_RUNNER_IMAGE,
                    image_pull_policy="Never",
                    env=[
                        client.V1EnvVar(name="TASK_ID", value=task_id),
                        client.V1EnvVar(name="TASK_MESSAGE", value=message),
                        client.V1EnvVar(name="MAX_STEPS", value=str(max_steps)),
                        client.V1EnvVar(name="WEBTOP_IMAGE", value=WEBTOP_IMAGE),
                        client.V1EnvVar(
                            name="ORCHESTRATOR_URL", value=ORCHESTRATOR_URL
                        ),
                        client.V1EnvVar(
                            name="POD_NAMESPACE",
                            value_from=client.V1EnvVarSource(
                                field_ref=client.V1ObjectFieldSelector(
                                    field_path="metadata.namespace"
                                )
                            ),
                        ),
                        client.V1EnvVar(
                            name="OPENAI_API_KEY",
                            value_from=client.V1EnvVarSource(
                                secret_key_ref=client.V1SecretKeySelector(
                                    name="openai-secret", key="api-key"
                                )
                            ),
                        ),
                    ],
                    resources=client.V1ResourceRequirements(
                        requests={"memory": "256Mi", "cpu": "100m"},
                        limits={"memory": "512Mi", "cpu": "500m"},
                    ),
                ),
            ],
        ),
    )

    k8s.create_namespaced_pod(namespace=NAMESPACE, body=pod)
    return pod_name


def _create_vnc_service(task_id: str, node_port: int) -> str:
    """Create a NodePort Service exposing the webtop's VNC port."""
    svc_name = f"vnc-{task_id[:8]}"

    svc = client.V1Service(
        metadata=client.V1ObjectMeta(
            name=svc_name,
            namespace=NAMESPACE,
            labels={"app": "vnc", "task-id": task_id},
        ),
        spec=client.V1ServiceSpec(
            type="NodePort",
            selector={"app": "webtop", "task-id": task_id},
            ports=[
                client.V1ServicePort(
                    port=3000,
                    target_port=3000,
                    node_port=node_port,
                    name="vnc",
                ),
            ],
        ),
    )

    k8s.create_namespaced_service(namespace=NAMESPACE, body=svc)
    return svc_name


def _delete_resource_safe(delete_fn, name: str):
    try:
        delete_fn(name, NAMESPACE)
    except client.exceptions.ApiException:
        pass


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/tasks", response_model=Task)
def create_task(req: TaskCreate):
    task_id = uuid.uuid4().hex
    vnc_port = _allocate_vnc_port()
    host_port = vnc_port - VNC_PORT_MIN + 6901

    runner_pod = _create_runner_pod(task_id, req.message, req.max_steps)
    webtop_pod = f"webtop-{task_id[:8]}"
    vnc_svc = f"vnc-{task_id[:8]}"

    # Create the VNC service (webtop pod will match the selector once the
    # task-runner creates it)
    _create_vnc_service(task_id, vnc_port)

    task = {
        "id": task_id,
        "message": req.message,
        "status": "pending",
        "created_at": _now(),
        "updated_at": _now(),
        "runner_pod": runner_pod,
        "webtop_pod": webtop_pod,
        "vnc_port": vnc_port,
        "vnc_svc": vnc_svc,
        "vnc_url": f"http://localhost:{host_port}",
        "result": None,
        "steps": [],
    }
    _save(task)
    return task


@app.get("/tasks", response_model=list[Task])
def list_tasks():
    return [json.loads(f.read_text()) for f in sorted(TASKS_DIR.glob("*.json"))]


@app.get("/tasks/{task_id}", response_model=Task)
def get_task(task_id: str):
    return _load(task_id)


@app.post("/tasks/{task_id}/callback")
def task_callback(task_id: str, cb: TaskCallback):
    task = _load(task_id)
    task["status"] = cb.status
    if cb.result is not None:
        task["result"] = cb.result
    if cb.steps is not None:
        task["steps"] = cb.steps
    task["updated_at"] = _now()
    _save(task)
    return {"ok": True}


@app.delete("/tasks/{task_id}")
def delete_task(task_id: str):
    task = _load(task_id)
    _delete_resource_safe(k8s.delete_namespaced_pod, task["runner_pod"])
    _delete_resource_safe(k8s.delete_namespaced_pod, task["webtop_pod"])
    if task.get("vnc_svc"):
        _delete_resource_safe(k8s.delete_namespaced_service, task["vnc_svc"])
    (TASKS_DIR / f"{task_id}.json").unlink(missing_ok=True)
    return {"ok": True}
