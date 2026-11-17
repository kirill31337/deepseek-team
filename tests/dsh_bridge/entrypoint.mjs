#!/usr/bin/env node
// Fixture hook entrypoint for the DSH bridge tests. Reads one JSON payload on
// stdin and emits canned coordinator-hook responses; never touches the network.
let raw = '';
process.stdin.on('data', (chunk) => { raw += chunk; });
process.stdin.on('end', () => {
    const payload = JSON.parse(raw);
    const event = payload.hook_event_name;
    const out = (value) => { process.stdout.write(JSON.stringify(value)); };
    if (event === 'SessionStart') {
        out({ hookSpecificOutput: { hookEventName: 'SessionStart', additionalContext: 'SESSION-CTX' } });
        return;
    }
    if (event === 'UserPromptSubmit') {
        if (payload.prompt === 'fail') {
            process.stderr.write('ledger fixture failure');
            process.exit(2);
        }
        out({ hookSpecificOutput: { hookEventName: 'UserPromptSubmit', additionalContext: 'PROMPT-CTX' } });
        return;
    }
    if (event === 'PreToolUse') {
        if (payload.tool_name === 'deny_me') {
            out({ hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'deny', permissionDecisionReason: 'FIXTURE-DENY' } });
            return;
        }
        if (payload.tool_name === 'nudge') {
            out({ hookSpecificOutput: { hookEventName: 'PreToolUse', additionalContext: 'NUDGE-CTX' } });
            return;
        }
        if (payload.tool_name === 'boom') {
            process.stderr.write('fixture hook crash');
            process.exit(1);
        }
        out({});
        return;
    }
    if (event === 'Stop') {
        if (payload.stop_hook_active) {
            out({ systemMessage: 'FIXTURE-WARN' });
        } else {
            out({ decision: 'block', reason: 'KEEP-WORKING' });
        }
        return;
    }
    out({});
});