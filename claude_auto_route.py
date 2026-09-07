#!/usr/bin/env python3
"""SessionStart: Claude 메인 세션에만 자동 Codex 구현 라우팅 정책을 주입한다."""
import codex_worker
import harness_lib as lib


def routing_context(config):
    route = config["routing"]
    planner = config["planner"]
    worker = config["worker"]
    bypass = ", ".join('"%s"' % phrase for phrase in route["bypass_phrases"])
    if route.get("enforcement", "hard") != "hard":
        return """[Claude main: Codex delegation available, advisory]
Work directly as usual at {planner} effort. Per-request hints may mark a request as a code change
or investigation; they are suggestions, not routes. Delegate to a Codex worker only when the user
asks with `{forced}` or the work is clearly long: `{model}`/{effort} for implementation,
`{inspect}` for read-only inspection. Start it with `codex_worker.py run ... --manifest - --detach`
and collect with `codex_worker.py wait <run_id>` in the foreground; never poll or background it.
Never delegate again inside Codex, and never commit, push, open a PR, or deploy unless asked.
""".format(planner=planner["default_effort"], forced=route["forced_command"],
           model=worker["model"], effort=worker["default_effort"],
           inspect=worker["inspection_effort"])
    return """[Claude main only: hard Codex handoff v2]
UserPromptSubmit supplies the route for each request; obey it. The user needs no slash command.
Main Fable is a bounded decision layer ({planner}, at most {turns} turns), not a developer or repo
explorer. CODEX_IMPLEMENT and CODEX_INSPECT allow only compact contract creation plus one blocking
worker call. Do not poll, background, redirect logs, edit, re-verify, or re-explore after handoff.
Use `{model}`/{effort} for implementation, `{inspect}` for read-only inspection, and `{escalation}`
only as measured escalation. Use fable-architect once only when the route explicitly requests it.
Explicit Claude-only bypass phrases: {bypass}. `{forced}` forces delegation. Never delegate again
inside Codex, and never commit, push, open a PR, or deploy unless the user requested it.
""".format(
        planner=planner["default_effort"],
        turns=planner["max_turns_before_handoff"],
        bypass=bypass,
        model=worker["model"],
        effort=worker["default_effort"],
        inspect=worker["inspection_effort"],
        escalation=worker["escalation_effort"],
        forced=route["forced_command"],
    )


def main():
    data = lib.read_hook_input()
    cwd = data.get("cwd", "")
    if not lib.in_workspace(cwd):
        return
    text = routing_context(codex_worker.load_config())
    lib.event("auto-route-context", data.get("session_id", ""), str(len(text)))
    print(lib.hook_output("SessionStart", text))


if __name__ == "__main__":
    lib.run_fail_open(main)
