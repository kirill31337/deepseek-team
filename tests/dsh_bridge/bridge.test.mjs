// DSH bridge plugin tests: a fake cordis context plus a fixture hook entrypoint.
// Run with: node --test tests/dsh_bridge/
import test from 'node:test';
import assert from 'node:assert/strict';
import { chmodSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const entrypoint = path.join(here, 'entrypoint.mjs');
chmodSync(entrypoint, 0o755);
process.env.PATH = path.dirname(process.execPath) + path.delimiter + (process.env.PATH || '');

const plugin = await import('../../src/codex_deepseek_team/data/dsh-bridge/index.js');

function makeAgent(id = 'sess-1', header = {}) {
    return {
        session: { id, header },
        injected: [],
        steered: [],
        inject(message) { this.injected.push(message); },
        steer(message) { this.steered.push(message); },
    };
}

function makeCtx(rootAgent) {
    const listeners = {};
    return {
        listeners,
        agents: { roots: () => [rootAgent] },
        workingDirectory: { get: () => '/repo' },
        on(event, fn) { listeners[event] = fn; },
    };
}

function userMessage(id, text) {
    return { id, role: 'user', content: [{ type: 'text', text }], source: { kind: 'user' } };
}

function execFor(agent, name, args = {}) {
    return {
        name, arguments: args, callId: 'call-1', token: 'token-1', agent,
        signal: new AbortController().signal,
    };
}

const allow = async () => ({ kind: 'allow' });

async function setup() {
    const agent = makeAgent();
    const ctx = makeCtx(agent);
    plugin.apply(ctx, { entrypoint });
    await ctx.listeners['agent/created']({ agent, source: 'startup' });
    return { agent, ctx };
}

test('apply rejects a missing or relative entrypoint', () => {
    assert.throws(() => plugin.apply(makeCtx(makeAgent()), {}));
    assert.throws(() => plugin.apply(makeCtx(makeAgent()), { entrypoint: 'relative/path' }));
});

test('SessionStart injects hook context for root agents only', async () => {
    const { agent, ctx } = await setup();
    assert.equal(agent.injected.length, 1);
    assert.equal(agent.injected[0].content[0].text, 'SESSION-CTX');
    assert.equal(agent.injected[0].source.kind, 'deepseek-team');

    const child = makeAgent('child-1', { parentSession: 'sess-1', origin: 'subagent' });
    await ctx.listeners['agent/created']({ agent: child, source: 'startup' });
    assert.equal(child.injected.length, 0);
    const decision = await ctx.listeners['tools/pre-execute'](execFor(child, 'deny_me'), allow);
    assert.deepEqual(decision, { kind: 'allow' });
});

test('UserPromptSubmit appends context once per fresh user message', async () => {
    const { agent, ctx } = await setup();
    const message = userMessage('m1', 'hello');
    const next = async () => ({ kind: 'enter', messages: [message] });
    const first = await ctx.listeners['agent/pre-step'](
        { agent, messages: [message], turn: 1, step: 1 }, next);
    assert.equal(first.kind, 'enter');
    assert.equal(first.messages.length, 2);
    assert.equal(first.messages[1].content[0].text, 'PROMPT-CTX');

    const again = await ctx.listeners['agent/pre-step'](
        { agent, messages: [message], turn: 1, step: 2 }, next);
    assert.equal(again.messages.length, 1);

    const steering = { id: 's1', role: 'user', content: [{ type: 'text', text: 'steer' }], source: { kind: 'plugin' } };
    const passthrough = await ctx.listeners['agent/pre-step'](
        { agent, messages: [steering], turn: 1, step: 3 }, next);
    assert.equal(passthrough.messages.length, 1);
});

test('a failed prompt marks the session ledger-down and denies tools until recovery', async () => {
    const { agent, ctx } = await setup();
    const next = async () => ({ kind: 'enter', messages: [userMessage('m1', 'fail')] });
    const decision = await ctx.listeners['agent/pre-step'](
        { agent, messages: [userMessage('m1', 'fail')], turn: 1, step: 1 }, next);
    assert.equal(decision.kind, 'enter');
    assert.match(decision.messages.at(-1).content[0].text, /could not initialize/);

    const denied = await ctx.listeners['tools/pre-execute'](execFor(agent, 'read'), allow);
    assert.equal(denied.kind, 'deny');
    assert.match(denied.reason, /ledger is unavailable/);

    const ok = userMessage('m2', 'hello again');
    await ctx.listeners['agent/pre-step'](
        { agent, messages: [ok], turn: 2, step: 1 }, async () => ({ kind: 'enter', messages: [ok] }));
    const allowed = await ctx.listeners['tools/pre-execute'](execFor(agent, 'read'), allow);
    assert.deepEqual(allowed, { kind: 'allow' });
});

test('PreToolUse deny, nudge delivery and fail-closed hook errors', async () => {
    const { agent, ctx } = await setup();
    const denied = await ctx.listeners['tools/pre-execute'](execFor(agent, 'deny_me'), allow);
    assert.equal(denied.kind, 'deny');
    assert.equal(denied.reason, 'FIXTURE-DENY');

    const nudged = execFor(agent, 'nudge');
    const allowed = await ctx.listeners['tools/pre-execute'](nudged, allow);
    assert.deepEqual(allowed, { kind: 'allow' });
    const post = await ctx.listeners['tools/post-execute'](
        nudged, { kind: 'accept', content: [] },
        async () => ({ kind: 'accept', content: [] }));
    assert.equal(post.additionalContexts.length, 1);
    assert.equal(post.additionalContexts[0].content[0].text, 'NUDGE-CTX');
    // The nudge is delivered once.
    const postAgain = await ctx.listeners['tools/post-execute'](
        nudged, { kind: 'accept', content: [] },
        async () => ({ kind: 'accept', content: [] }));
    assert.equal(postAgain.additionalContexts, undefined);

    const crashed = await ctx.listeners['tools/pre-execute'](execFor(agent, 'boom'), allow);
    assert.equal(crashed.kind, 'deny');
    assert.match(crashed.reason, /failed closed/);
});

test('Stop blocks once via steer, then warns without blocking again', async () => {
    const { agent, ctx } = await setup();
    await ctx.listeners['agent/turn-stopping']({ agent, turn: 1 });
    assert.equal(agent.steered.length, 1);
    assert.equal(agent.steered[0].content[0].text, 'KEEP-WORKING');
    await ctx.listeners['agent/turn-stopping']({ agent, turn: 1 });
    assert.equal(agent.steered.length, 1);
    assert.match(agent.injected.at(-1).content[0].text, /FIXTURE-WARN/);
    // A fresh user prompt resets the stop cap.
    const message = userMessage('m2', 'next task');
    await ctx.listeners['agent/pre-step'](
        { agent, messages: [message], turn: 2, step: 1 },
        async () => ({ kind: 'enter', messages: [message] }));
    await ctx.listeners['agent/turn-stopping']({ agent, turn: 2 });
    assert.equal(agent.steered.length, 2);
});

test('disposed agents are unbound', async () => {
    const { agent, ctx } = await setup();
    ctx.listeners['agent/disposed']({ agent });
    const decision = await ctx.listeners['tools/pre-execute'](execFor(agent, 'deny_me'), allow);
    assert.deepEqual(decision, { kind: 'allow' });
});