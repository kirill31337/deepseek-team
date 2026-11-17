/**
 * DeepSeek Team bridge for the Qwen coordinator inside the DeepSeek Harness.
 *
 * Dependency-free ESM cordis plugin. It binds ROOT agents only and translates
 * DSH lifecycle events into `deepseek-team coordinator-hook --runtime qwen`
 * subprocess calls, applying the returned decisions:
 *
 *   agent/created        -> SessionStart   (context via agent.inject)
 *   agent/pre-step       -> UserPromptSubmit for fresh user-source messages
 *                           (context appended to the admitted step batch)
 *   tools/pre-execute    -> PreToolUse     (deny / early-planning nudge)
 *   tools/post-execute   -> delivers a stored PreToolUse nudge as context
 *   agent/turn-stopping  -> Stop           (block -> agent.steer continuation)
 *
 * Fail-closed contract (Claude parity): a failed PreToolUse hook denies the
 * call; a failed UserPromptSubmit marks the session ledger-down and every tool
 * call is denied until a later prompt initializes the ledger again. The step
 * itself is never rejected, because a DSH pre-step rejection would consume the
 * user prompt without processing it. SessionStart and Stop hook failures are
 * nonblocking. Native subagents/team members never touch the coordinator
 * ledger: child agents are not bound at all.
 *
 * The plugin never reads credentials, never logs payloads, and bounds every
 * subprocess stream. Configuration comes from the owned profile patch row:
 *   config: { entrypoint: <absolute deepseek-team path>, timeoutMs?: number }
 */
import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';

export const name = 'deepseek-team-qwen';

const RUNTIME = 'qwen';
const DEFAULT_TIMEOUT_MS = 10_000;
const MAX_OUTPUT_BYTES = 1024 * 1024;
const MAX_DETAIL_CHARS = 400;
const SOURCE = { kind: 'deepseek-team' };

function detail(error) {
    const text = String(error && error.message ? error.message : error)
        .replace(/\s+/g, ' ').trim();
    return text.length > MAX_DETAIL_CHARS ? text.slice(0, MAX_DETAIL_CHARS - 3) + '...' : text;
}

function userMessage(text, form) {
    return {
        id: randomUUID(),
        role: 'user',
        content: [{ type: 'text', text }],
        source: { ...SOURCE, form },
    };
}

function isChildAgent(ctx, agent) {
    try {
        if (typeof ctx.agents?.roots === 'function'
            && !ctx.agents.roots().includes(agent)) return true;
    } catch { /* registry unavailable: fall back to header evidence */ }
    const header = agent?.session?.header;
    return Boolean(header && (header.parentSession || header.origin === 'subagent'
        || (header.delegationDepth || 0) > 0));
}

function sessionCwd(ctx, agent) {
    try {
        const cwd = ctx.workingDirectory?.get(agent.session);
        if (typeof cwd === 'string' && cwd) return cwd;
    } catch { /* fall back to creation metadata */ }
    return agent?.session?.header?.cwd || process.cwd();
}

/** One bounded hook subprocess; resolves the parsed JSON object or null. */
function callHook(entrypoint, payload, timeoutMs, signal) {
    return new Promise((resolve, reject) => {
        let child;
        try {
            child = spawn(entrypoint, ['coordinator-hook', '--runtime', RUNTIME], {
                stdio: ['pipe', 'pipe', 'pipe'],
                env: process.env,
            });
        } catch (error) {
            reject(new Error('hook process could not start: ' + detail(error)));
            return;
        }
        let stdout = '';
        let stderr = '';
        let settled = false;
        const finish = (error, value) => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            if (signal) signal.removeEventListener('abort', onAbort);
            if (error) reject(error); else resolve(value);
        };
        const onAbort = () => {
            try { child.kill('SIGKILL'); } catch { /* already gone */ }
            finish(new Error('hook call cancelled'));
        };
        const timer = setTimeout(() => {
            try { child.kill('SIGKILL'); } catch { /* already gone */ }
            finish(new Error(`hook exceeded ${timeoutMs}ms`));
        }, timeoutMs);
        if (signal) {
            if (signal.aborted) { onAbort(); return; }
            signal.addEventListener('abort', onAbort, { once: true });
        }
        child.stdout.on('data', (chunk) => {
            if (stdout.length < MAX_OUTPUT_BYTES) stdout += chunk.toString('utf8');
        });
        child.stderr.on('data', (chunk) => {
            if (stderr.length < MAX_OUTPUT_BYTES) stderr += chunk.toString('utf8');
        });
        child.on('error', (error) => finish(new Error('hook process failed: ' + detail(error))));
        child.on('close', (code) => {
            if (code !== 0) {
                finish(new Error(`hook exited ${code}: ${detail(stderr)}`));
                return;
            }
            const text = stdout.trim();
            if (!text) { finish(null, null); return; }
            try {
                const value = JSON.parse(text);
                finish(null, value && typeof value === 'object' && !Array.isArray(value) ? value : null);
            } catch (error) {
                finish(new Error('hook returned malformed JSON: ' + detail(error)));
            }
        });
        try {
            child.stdin.on('error', () => { /* close handler reports the failure */ });
            child.stdin.end(JSON.stringify(payload));
        } catch (error) {
            try { child.kill('SIGKILL'); } catch { /* already gone */ }
            finish(new Error('hook input could not be written: ' + detail(error)));
        }
    });
}

export function apply(ctx, config) {
    const entrypoint = config?.entrypoint;
    if (typeof entrypoint !== 'string' || !entrypoint.startsWith('/')) {
        throw new Error('deepseek-team-qwen requires an absolute entrypoint path in config');
    }
    const timeoutMs = Number.isFinite(config?.timeoutMs) && config.timeoutMs > 0
        ? Math.min(config.timeoutMs, 120_000) : DEFAULT_TIMEOUT_MS;

    /** Per-root-agent coordination state; child agents are never bound. */
    const bound = new Map();

    const base = (agent) => ({
        session_id: String(agent.session.id),
        cwd: sessionCwd(ctx, agent),
    });

    ctx.on('agent/created', async ({ agent, source }) => {
        if (isChildAgent(ctx, agent)) return;
        const state = {
            seenPrompts: new Set(),
            stopActive: false,
            ledgerDown: false,
            nudges: new Map(),
        };
        bound.set(agent, state);
        try {
            const result = await callHook(entrypoint, {
                hook_event_name: 'SessionStart',
                ...base(agent),
                source: source || 'startup',
            }, timeoutMs);
            const text = result?.hookSpecificOutput?.additionalContext;
            if (typeof text === 'string' && text) agent.inject(userMessage(text, 'snapshot'));
        } catch (error) {
            // SessionStart is nonblocking (Claude parity); the first prompt
            // still initializes the ledger through UserPromptSubmit.
            console.error('[deepseek-team-qwen] SessionStart hook error: ' + detail(error));
        }
    });

    ctx.on('agent/disposed', ({ agent }) => {
        bound.delete(agent);
    });

    ctx.on('agent/pre-step', async ({ agent, messages, turn }, next) => {
        const state = bound.get(agent);
        if (!state) return next();
        const fresh = messages.filter((message) =>
            message?.source?.kind === 'user' && !state.seenPrompts.has(message.id));
        if (!fresh.length) return next();
        for (const message of fresh) state.seenPrompts.add(message.id);
        state.stopActive = false;
        const prompt = fresh.map((message) => (message.content || [])
            .filter((block) => block?.type === 'text' && typeof block.text === 'string')
            .map((block) => block.text).join('\n')).join('\n');
        let result = null;
        let failure = null;
        try {
            result = await callHook(entrypoint, {
                hook_event_name: 'UserPromptSubmit',
                ...base(agent),
                prompt,
                turn_id: String(turn),
            }, timeoutMs);
            state.ledgerDown = false;
        } catch (error) {
            failure = error;
            state.ledgerDown = true;
        }
        const decision = await next();
        if (decision?.kind !== 'enter') return decision;
        const extra = [];
        if (failure) {
            extra.push(userMessage(
                'DeepSeek Team could not initialize the coordination ledger for this prompt: '
                + detail(failure) + '. Tool calls are denied until a new prompt initializes '
                + 'the ledger. Check local DeepSeek Team settings and state, then submit '
                + 'the prompt again.', 'notice'));
        } else {
            const text = result?.hookSpecificOutput?.additionalContext;
            if (typeof text === 'string' && text) extra.push(userMessage(text, 'snapshot'));
        }
        return extra.length ? { ...decision, messages: [...decision.messages, ...extra] } : decision;
    });

    ctx.on('tools/pre-execute', async (exec, next) => {
        const state = exec.agent ? bound.get(exec.agent) : undefined;
        if (!state) return next();
        if (state.ledgerDown) {
            return {
                kind: 'deny',
                reason: 'DeepSeek Team coordination ledger is unavailable for this session; '
                    + 'submit a new prompt after repairing local DeepSeek Team state.',
            };
        }
        let result;
        try {
            result = await callHook(entrypoint, {
                hook_event_name: 'PreToolUse',
                ...base(exec.agent),
                tool_name: exec.name,
                tool_input: exec.arguments ?? {},
                tool_use_id: String(exec.callId),
            }, timeoutMs, exec.signal);
        } catch (error) {
            if (exec.signal?.aborted) return { kind: 'cancel' };
            // Fail closed exactly like the Claude PreToolUse gate.
            return {
                kind: 'deny',
                reason: 'DeepSeek Team blocked this tool call because the PreToolUse hook '
                    + 'failed closed: ' + detail(error) + '. The requested operation was not '
                    + 'approved. Check local DeepSeek Team settings and state, then retry.',
            };
        }
        const specific = result?.hookSpecificOutput;
        if (specific?.permissionDecision === 'deny') {
            return {
                kind: 'deny',
                reason: typeof specific.permissionDecisionReason === 'string'
                    && specific.permissionDecisionReason
                    ? specific.permissionDecisionReason
                    : 'Denied by DeepSeek Team coordination policy.',
            };
        }
        if (typeof specific?.additionalContext === 'string' && specific.additionalContext) {
            state.nudges.set(exec.token, specific.additionalContext);
        }
        return next();
    });

    ctx.on('tools/post-execute', async (exec, _result, next) => {
        const decision = await next();
        const state = exec.agent ? bound.get(exec.agent) : undefined;
        const nudge = state?.nudges.get(exec.token);
        if (!nudge) return decision;
        state.nudges.delete(exec.token);
        const message = userMessage(nudge, 'notice');
        if (decision?.kind === 'block') {
            return {
                ...decision,
                additionalContexts: [message, ...(decision.additionalContexts || [])],
            };
        }
        if (decision?.kind === 'accept') {
            return {
                ...decision,
                additionalContexts: [message, ...(decision.additionalContexts || [])],
            };
        }
        return decision;
    });

    ctx.on('agent/turn-stopping', async ({ agent }) => {
        const state = bound.get(agent);
        if (!state) return;
        let result;
        try {
            result = await callHook(entrypoint, {
                hook_event_name: 'Stop',
                ...base(agent),
                stop_hook_active: state.stopActive,
            }, timeoutMs);
        } catch (error) {
            console.error('[deepseek-team-qwen] Stop hook error (nonblocking): ' + detail(error));
            return;
        }
        if (result?.decision === 'block' && typeof result.reason === 'string' && result.reason) {
            if (!state.stopActive) {
                state.stopActive = true;
                agent.steer(userMessage(result.reason, 'notice'));
            } else {
                // Bound our own retry without vetoing another listener's
                // continuation; the ledger stays unfinished.
                agent.inject(userMessage(
                    'DeepSeek Team task remains unfinished. ' + result.reason, 'notice'));
            }
            return;
        }
        if (typeof result?.systemMessage === 'string' && result.systemMessage) {
            agent.inject(userMessage(result.systemMessage, 'notice'));
        }
    });
}