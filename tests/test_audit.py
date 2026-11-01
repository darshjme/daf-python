"""Independent adversarial acceptance of authorization and process boundaries."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from daf_adapter.protocol import MAX_REQUEST_BYTES, validate_request, validate_result
from daf_adapter.runner import run
from daf_adapter.workflow import execute_workflow


def request(ident="audit", **changes):
    return {"schema_version": 1, "id": ident, "argv": [sys.executable, "-c", "print('ok')"], **changes}


def success(ident):
    return {"schema_version": 1, "id": ident, "status": "succeeded", "exit_code": 0,
            "stdout": "", "stderr": "", "stdout_truncated": False, "stderr_truncated": False}


class AdmissionAudit(unittest.TestCase):
    def test_normalized_size_boundary(self):
        base = validate_request(request(stdin=""))
        overhead = len(json.dumps(base, ensure_ascii=False, separators=(",", ":")).encode())
        base["stdin"] = "a" * (MAX_REQUEST_BYTES - overhead)
        self.assertEqual(len(validate_request(base)["stdin"]), MAX_REQUEST_BYTES - overhead)
        base["stdin"] += "a"
        with self.assertRaises(ValueError):
            validate_request(base)

    def test_invalid_request_boundaries(self):
        for changes in ({"id": "\u2003"}, {"id": "é" * 65}, {"cwd": "/missing-daf-audit-cwd"},
                        {"timeout_ms": True}, {"stdin": "\ud800"}, {"argv": ["echo", "\0"]}):
            with self.subTest(changes=repr(changes)), self.assertRaises(ValueError):
                validate_request(request(**changes))

    def test_forged_results_are_rejected(self):
        for patch in ({"exit_code": True}, {"exit_code": 7}, {"id": "other"},
                      {"schema_version": True}, {"stdout_truncated": 0}, {"extra": "ignored?"}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                validate_result({**success("audit"), **patch}, "audit")


class RuntimeAudit(unittest.IsolatedAsyncioTestCase):
    async def test_approval_mutation_cannot_change_request(self):
        seen = []
        async def approve(req):
            req["argv"] = ["not-approved"]
            return True
        async def runner(req):
            seen.append(req["argv"])
            return success(req["id"])
        original = request()
        result = await execute_workflow([{"request": original}], approve, runner=runner)
        self.assertEqual(seen, [original["argv"]])
        self.assertEqual(result["audit"]["status"], "succeeded")

    async def test_forged_success_blocks_dependency(self):
        calls = []
        async def approve(req):
            return True
        async def runner(req):
            calls.append(req["id"])
            return {**success(req["id"]), "exit_code": 12}
        result = await execute_workflow([
            {"request": request("a")}, {"request": request("b"), "dependencies": ["a"]}
        ], approve, runner=runner)
        self.assertEqual(calls, ["a"])
        self.assertEqual(result["a"]["status"], "failed")
        self.assertEqual(result["b"]["status"], "skipped")

    async def test_literal_argv_does_not_expand_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "should-not-exist"
            text = "$(touch " + str(marker) + "); * $HOME"
            result = await run(request(argv=[sys.executable, "-c", "import sys;print(sys.argv[1])", text]))
            self.assertEqual(result["stdout"], text + "\n")
            self.assertFalse(marker.exists())

    async def test_spawn_error_obeys_small_capture(self):
        result = await run(request(argv=["/no-such-daf-audit-executable"], max_output_bytes=3))
        self.assertEqual(result["status"], "failed")
        self.assertLessEqual(len(result["stderr"].encode()), 9)
        self.assertTrue(result["stderr_truncated"])

    @unittest.skipUnless(os.name == "posix", "POSIX child liveness probe")
    async def test_cancelled_child_is_reaped(self):
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory) / "pid"
            code = "import os,time;from pathlib import Path;Path(" + repr(str(pidfile)) + ").write_text(str(os.getpid()));time.sleep(30)"
            task = asyncio.create_task(run(request(argv=[sys.executable, "-c", code])))
            try:
                for _ in range(200):
                    if pidfile.exists():
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(pidfile.exists())
                pid = int(pidfile.read_text())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
