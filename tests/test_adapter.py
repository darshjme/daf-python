import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from daf_adapter import execute_workflow, run, validate_request


def request(code, **options):
    return dict(schema_version=1, id="test", argv=[sys.executable, "-c", code], **options)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_literal_arguments_stdin_and_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            value = request("import sys,os,json; print(json.dumps([sys.argv[1],sys.stdin.read(),os.getcwd()]))", stdin="hello\n", cwd=directory)
            value["argv"].append("$(touch SHOULD_NOT_EXIST); *")
            result = await run(value)
            self.assertEqual(result["status"], "succeeded")
            args = json.loads(result["stdout"])
            self.assertEqual(args[:2], ["$(touch SHOULD_NOT_EXIST); *", "hello\n"])
            self.assertEqual(Path(args[2]).resolve(), Path(directory).resolve())
            self.assertFalse(Path(directory, "SHOULD_NOT_EXIST").exists())

    async def test_both_streams_drained_but_bounded(self):
        result = await run(request("import os; os.write(1,b'x'*200000); os.write(2,b'y'*200000)", max_output_bytes=97))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["stdout"], "x" * 97)
        self.assertEqual(result["stderr"], "y" * 97)
        self.assertTrue(result["stdout_truncated"] and result["stderr_truncated"])

    async def test_failure_missing_command_timeout_and_utf8(self):
        self.assertEqual((await run(request("raise SystemExit(7)")))["exit_code"], 7)
        value = request(""); value["argv"] = ["/nonexistent/daf-test-executable"]
        self.assertEqual((await run(value))["status"], "failed")
        self.assertEqual((await run(request("import time; time.sleep(3)", timeout_ms=50)))["status"], "timed_out")
        result = await run(request("import os; os.write(1,bytes([255,226,130,172]))", max_output_bytes=3))
        self.assertEqual(result["stdout"], "\ufffd\ufffd")
        self.assertTrue(result["stdout_truncated"])

    async def test_cancellation_reaps(self):
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory, "pid")
            task = asyncio.create_task(run(request(f"import os,time; open({str(pidfile)!r},'w').write(str(os.getpid())); time.sleep(30)")))
            for _ in range(100):
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

    async def test_workflow_denial_failure_and_no_invalid_side_effects(self):
        called = []
        async def approve(value):
            called.append(value["id"])
            return value["id"] != "denied"
        def node(ident, deps=(), code="print('ok')"):
            value = request(code); value["id"] = ident
            return {"request": value, "dependencies": list(deps)}
        nodes = [node("denied"), node("skip", ["denied"]), node("good"), node("fail", code="raise SystemExit(1)"), node("skip2", ["fail"])]
        result = await execute_workflow(nodes, approve, concurrency=2)
        self.assertEqual([v["status"] for v in result.values()], ["denied", "skipped", "succeeded", "failed", "skipped"])
        self.assertEqual(set(called), {"denied", "good", "fail"})
        called.clear()
        with self.assertRaises(ValueError):
            await execute_workflow([node("a", ["b"]), node("b", ["a"])], approve)
        self.assertEqual(called, [])

    async def test_approval_cannot_mutate_command(self):
        async def approve(value):
            value["argv"] = ["nonexistent"]
            return True
        result = await execute_workflow([{"request": request("print('original')")}], approve)
        self.assertEqual(result["test"]["stdout"], "original\n")


class ValidationTests(unittest.TestCase):
    def test_bad_values(self):
        for key, val in [("schema_version", True), ("id", ""), ("id", "é"*65), ("argv", []), ("argv", ["a\0b"]), ("timeout_ms", True), ("timeout_ms", 0), ("timeout_ms", 1.5), ("max_output_bytes", 1048577), ("cwd", ""), ("unknown", 1)]:
            with self.subTest(key=key, value=val), self.assertRaises(ValueError):
                value = request(""); value[key] = val; validate_request(value)
        with self.assertRaises(ValueError):
            validate_request(request("", stdin="x" * 1048576))


if __name__ == "__main__":
    unittest.main()
