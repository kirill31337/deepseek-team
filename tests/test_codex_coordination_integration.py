"""Real Codex coordinator lifecycle-hook integration against an offline Responses fixture."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
import threading
import unittest
import unittest.mock

from codex_deepseek_team import config, coordination, project, settings


class RealCodexCoordinatorHookTests(unittest.TestCase):
    def _codex(self):
        binary = shutil.which("codex")
        if binary:
            return binary
        if os.environ.get("DEEPSEEK_TEAM_REQUIRE_LIVE") == "1":
            self.fail("Required Codex CLI is unavailable")
        self.skipTest("Codex CLI is not installed")

    def test_new_session_registers_distribution_before_blocked_duplicate_mutation(self):
        binary = self._codex()
        requests = []
        tool_outputs = []
        state = {"step": 0, "task_id": None, "assignment_id": None}
        synthetic_key = secrets.token_hex(24)

        # Codex may finish an async plugin-cache cleanup just after the main process exits.
        # Cleanup races are not part of the coordinator-hook assertion surface.
        with tempfile.TemporaryDirectory(prefix="dst-codex-coordinator-", ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            home = root / "codex-home"
            home.mkdir(mode=0o700)
            repo = root / "repo"
            repo.mkdir()
            (repo / "a.py").write_text("VALUE = 1\n")
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run([
                "git", "-C", str(repo), "-c", "user.name=Fixture",
                "-c", "user.email=fixture@example.invalid", "commit", "-qm", "base"
            ], check=True)
            settings.set_values(repo / settings.PROJECT_FILE,
                                delegation_level=75, access="full-access")
            project.attach(repo, coordinator="codex")
            config.install_codex_hooks(home)

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass

                def do_POST(self):
                    raw = self.rfile.read(int(self.headers["Content-Length"]))
                    body = json.loads(raw)
                    requests.append({"path": self.path, "model": body.get("model")})
                    for item in body.get("input", []):
                        if isinstance(item, dict) and item.get("type") in (
                                "function_call_output", "custom_tool_call_output"):
                            tool_outputs.append(str(item.get("output", "")))

                    serialized = json.dumps(body)
                    match = re.search(r"task-[0-9a-f]{20}", serialized)
                    if match and state["task_id"] is None:
                        state["task_id"] = match.group(0)

                    tools = {item.get("name") for item in body.get("tools", [])
                             if isinstance(item, dict)}
                    step = state["step"]
                    state["step"] += 1

                    def shell_call(command, number):
                        if "exec_command" in tools:
                            name = "exec_command"
                            arguments = {"cmd": command, "max_output_tokens": 2000}
                        elif "shell_command" in tools:
                            name = "shell_command"
                            arguments = {"command": command, "timeout_ms": 10000}
                        else:
                            name = "shell"
                            arguments = {"command": ["sh", "-c", command],
                                         "timeout_ms": 10000}
                        return {
                            "type": "function_call", "id": f"fc_{number}",
                            "call_id": f"call_{number}", "name": name,
                            "arguments": json.dumps(arguments), "status": "completed",
                        }

                    if step == 0:
                        if state["task_id"] is None:
                            output = [{
                                "type": "message", "id": "msg_missing", "role": "assistant",
                                "status": "completed", "content": [{
                                    "type": "output_text",
                                    "text": "TASK_ID_NOT_IN_CONTEXT", "annotations": []
                                }]
                            }]
                        else:
                            plan = {
                                "classification": "substantial",
                                "deliverables": [{
                                    "id": "impl", "kind": "implementation",
                                    "scope": ["a.py"], "executor": "worker",
                                    "acceptance": ["VALUE becomes 2"],
                                    "dependencies": [], "checks": []
                                }]
                            }
                            command = (
                                "printf %s " + shlex.quote(json.dumps(plan)) +
                                " | deepseek-team coordination plan --path " +
                                shlex.quote(str(repo)) + " --task " + state["task_id"]
                            )
                            output = [shell_call(command, 1)]
                    elif step == 1:
                        for value in tool_outputs:
                            match = re.search(r"as-[0-9a-f]{16}", value)
                            if match:
                                state["assignment_id"] = match.group(0)
                        output = [shell_call("printf 'VALUE = 999\\n' > a.py", 2)]
                    else:
                        output = [{
                            "type": "message", "id": f"msg_{step}", "role": "assistant",
                            "status": "completed", "content": [{
                                "type": "output_text",
                                "text": "COORDINATION_GATE_OBSERVED", "annotations": []
                            }]
                        }]

                    response = {
                        "id": f"resp_{step}", "object": "response", "model": "fixture",
                        "created_at": 0, "status": "completed", "output": output,
                        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    }
                    events = [("response.created", {
                        "response": dict(response, status="in_progress", output=[])
                    })]
                    for item in output:
                        events += [
                            ("response.output_item.added", {"output_index": 0, "item": item}),
                            ("response.output_item.done", {"output_index": 0, "item": item}),
                        ]
                    events.append(("response.completed", {"response": response}))
                    payload = "".join(
                        f"event: {kind}\ndata: {json.dumps(dict(data, type=kind, sequence_number=n))}\n\n"
                        for n, (kind, data) in enumerate(events)
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                (home / "config.toml").write_text(
                    'model = "fixture"\n'
                    'model_provider = "fixture"\n'
                    'approval_policy = "never"\n'
                    'model_reasoning_effort = "low"\n'
                    '[features]\nhooks = true\n'
                    '[model_providers.fixture]\n'
                    'name = "Fixture"\n'
                    f'base_url = "http://127.0.0.1:{server.server_port}/"\n'
                    'env_key = "FIXTURE_KEY"\n'
                    'wire_api = "responses"\n'
                    'requires_openai_auth = false\n'
                    'supports_websockets = false\n'
                    'request_max_retries = 0\n'
                    'stream_max_retries = 0\n'
                )
                env = dict(os.environ)
                env.update(
                    CODEX_HOME=str(home),
                    FIXTURE_KEY=synthetic_key,
                    DEEPSEEK_TEAM_STATE_DIR=str(root / "state"),
                )
                result = subprocess.run([
                    binary, "exec", "--strict-config", "--ephemeral", "--json",
                    "--sandbox", "danger-full-access", "--dangerously-bypass-hook-trust",
                    "-C", str(repo),
                    "Implement the feature. Follow all project and lifecycle-hook instructions."
                ], env=env, text=True, capture_output=True, timeout=45, check=False)

                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                self.assertGreaterEqual(len(requests), 3, result.stderr + result.stdout)
                self.assertIsNotNone(state["task_id"], "UserPromptSubmit context did not reach Codex")
                self.assertIsNotNone(state["assignment_id"], str(tool_outputs))
                self.assertEqual((repo / "a.py").read_text(), "VALUE = 1\n",
                                 "Coordinator mutation reached disk despite pending worker assignment")
                with unittest.mock.patch.dict(
                        os.environ, {"DEEPSEEK_TEAM_STATE_DIR": str(root / "state")}):
                    task = coordination.load_task(repo, state["task_id"])
                self.assertEqual(task["assignments"][0]["status"], "planned")
                self.assertEqual(task["assignments"][0]["id"], state["assignment_id"])
                combined = "\n".join(tool_outputs) + result.stdout + result.stderr
                self.assertRegex(combined.lower(), r"(deny|blocked|pending worker|distribution)")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
