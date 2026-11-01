"""A small portable DAG dispatcher with explicit authorization callbacks."""

import asyncio
import json

from .protocol import MAX_REQUEST_BYTES, validate_request, validate_result
from .runner import run


async def execute_workflow(nodes, approve, *, concurrency=1, runner=run):
    """Validate all nodes, then execute ready tasks in bounded dependency waves.

    Each node is {request: protocol_request, dependencies: [request_id, ...]}.
    approve(request) must return literal True; denial blocks dependent work.
    No request is retried. A callback exception fails its node, not its siblings.
    """
    if type(concurrency) is not int or not 1 <= concurrency <= 32:
        raise ValueError("concurrency must be 1..32")
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 256:
        raise ValueError("workflow must contain 1..256 nodes")
    raw = json.dumps(nodes, allow_nan=False, ensure_ascii=False).encode("utf-8")
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("workflow exceeds 1 MiB")
    snapshot = json.loads(raw)
    requests, dependencies = {}, {}
    for node in snapshot:
        if not isinstance(node, dict) or set(node) - {"request", "dependencies"}:
            raise ValueError("invalid workflow node")
        request = validate_request(node.get("request"))
        ident = request["id"]
        if ident in requests:
            raise ValueError("duplicate request id")
        deps = node.get("dependencies", [])
        if not isinstance(deps, list) or any(not isinstance(dep, str) for dep in deps) or len(set(deps)) != len(deps):
            raise ValueError("dependencies must be unique strings")
        requests[ident], dependencies[ident] = request, deps
    done = set()
    while len(done) < len(requests):
        ready = {ident for ident, deps in dependencies.items() if ident not in done and set(deps) <= done}
        if not ready:
            raise ValueError("workflow contains a cycle or unknown dependency")
        done.update(ready)
    results = {}

    async def dispatch(ident):
        request = requests[ident]
        # Approval receives a detached copy; edits cannot change execution.
        try:
            if await approve(json.loads(json.dumps(request))) is not True:
                return {"id": ident, "status": "denied"}
            result = await runner(json.loads(json.dumps(request)))
            return validate_result(result, ident)
        except Exception:
            return {"id": ident, "status": "failed", "error": "approval or runner callback failed"}

    while len(results) < len(requests):
        ready = [ident for ident, deps in dependencies.items() if ident not in results and all(dep in results for dep in deps)]
        selected = []
        for ident in ready:
            if any(results[dep]["status"] != "succeeded" for dep in dependencies[ident]):
                results[ident] = {"id": ident, "status": "skipped"}
            elif len(selected) < concurrency:
                selected.append(ident)
        if selected:
            values = await asyncio.gather(*(dispatch(ident) for ident in selected))
            results.update(zip(selected, values))
    return {ident: results[ident] for ident in requests}
