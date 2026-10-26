// Execute the actual client modules with a fake clock/network, not a second
// implementation of refresh. Browser/Web Locks integration is tested separately.
import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { createContext, Script } from "node:vm";
import { randomUUID } from "node:crypto";
import { setImmediate } from "node:timers/promises";
import ts from "typescript";
import type { BrowserSession } from "../src/lib/session-contracts";

type SessionModule = {
  ensureSession: (expected?: BrowserSession, minimum?: number) => Promise<BrowserSession>;
  confirmResultSession: (expected: BrowserSession) => Promise<BrowserSession>;
  watchSession: (initial: BrowserSession, error: (e: unknown) => void) => () => void;
};
function fixture() {
  let now = 1800000000000;
  let expiry = now + 900000;
  const slot = randomUUID();
  const current: BrowserSession = { slot, sessionId: randomUUID(), expiresAt: expiry,
    user: { id: randomUUID(), email: "clock@example.com", created_at: new Date(now).toISOString() } };
  const calls: string[] = [];
  const storage = new Map<string, string>();
  const timers = new Map<number, { delay: number; callback: () => void }>();
  let timerId = 0;
  let failure = "";
  let statusHook = () => {};
  let tail = Promise.resolve<unknown>(undefined);
  const window = new EventTarget();
  const document = Object.assign(new EventTarget(), { cookie: `knowflow_active=${slot}`, visibilityState: "visible" });
  const context = createContext({
    document, window, navigator: { locks: { request: (_: string, __: unknown, work: () => Promise<unknown>) => {
      const result = tail.then(work, work); tail = result.catch(() => {}); return result;
    } } },
    localStorage: { getItem: (key: string) => storage.get(key) ?? null,
      setItem: (key: string, value: string) => storage.set(key, value), removeItem: (key: string) => storage.delete(key) },
    Date: class extends Date { static now() { return now; } },
    crypto: { randomUUID }, Event, AbortSignal, FormData, location: { protocol: "http:" },
    setTimeout: (callback: () => void, delay: number) => { timers.set(++timerId, { callback, delay }); return timerId; },
    clearTimeout: (id: number) => timers.delete(id),
    fetch: async (path: string) => {
      calls.push(path);
      if (path === "/api/auth/refresh") {
        if (failure === "timeout") throw new DOMException("timeout", "TimeoutError");
        if (failure === "503") return Response.json({ message: "unavailable" }, { status: 503 });
        expiry = now + 900000;
        return Response.json({ ...current, expiresAt: expiry });
      }
      if (path === "/api/auth/me") {
        statusHook();
        if (failure === "me401") return Response.json({}, {status:401});
        if (failure === "me503") return Response.json({}, {status:503});
        return Response.json(now >= expiry ? { state: "refresh_required", slot } : { ...current, expiresAt: expiry });
      }
      return Response.json({ ok: true });
    },
  });
  const cache = new Map<string, { exports: unknown }>();
  function load(file: string): unknown {
    if (cache.has(file)) return cache.get(file)!.exports;
    const entry = { exports: {} }; cache.set(file, entry);
    const source = ts.transpileModule(readFileSync(file, "utf8"), {
      compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS },
    }).outputText;
    const execute = new Script(`(function(require,module,exports){${source}\n})`, { filename: file }).runInContext(context);
    execute((name: string) => load(resolve(dirname(file), `${name}.ts`)), entry, entry.exports);
    return entry.exports;
  }
  const session = load(resolve("src/lib/session-client.ts")) as SessionModule;
  return { session, current, calls, storage, timers, document,
    onStatus: (hook: () => void) => { statusHook = hook; },
    advance: (ms: number) => { now += ms; }, fail: (value: string) => { failure = value; } };
}
async function flush() { for (let i = 0; i < 8; i++) await setImmediate(); }
async function outcome(work: Promise<unknown>) {
  try { await work; return 200; }
  catch (error) { return (error as {status:number}).status; }
}

test("the real scheduler renews at 14 minutes and survives the original 15-minute deadline", async () => {
  const f = fixture(); const errors: unknown[] = [];
  const stop = f.session.watchSession(f.current, e => errors.push(e));
  await flush();
  expect([...f.timers.values()][0].delay).toBe(840000);
  f.advance(841000);
  [...f.timers.values()][0].callback(); await flush();
  expect(f.calls.filter(v => v.endsWith("refresh"))).toHaveLength(1);
  f.advance(60000); // Beyond the ORIGINAL Access JWT expiration.
  const same = await f.session.ensureSession(f.current);
  expect(same.sessionId).toBe(f.current.sessionId);
  expect(errors).toEqual([]); expect(f.storage.size).toBe(0); stop();
});

test("same-page concurrent preparations share one rotation after sleeping past expiry", async () => {
  const f = fixture(); f.advance(1000000);
  const sessions = await Promise.all(Array.from({length: 8}, () => f.session.ensureSession(f.current)));
  expect(f.calls.filter(v => v.endsWith("refresh"))).toHaveLength(1);
  expect(sessions.every(v => v.sessionId === f.current.sessionId)).toBe(true);
});

for (const failure of ["timeout", "503"]) {
  test(`${failure} marks rotation uncertain before releasing the lock and never retries`, async () => {
    const f = fixture(); f.advance(1000000); f.fail(failure);
    expect(await outcome(f.session.ensureSession(f.current))).toBe(failure === "timeout" ? 504 : 503);
    expect(await outcome(f.session.ensureSession(f.current))).toBe(409);
    expect(f.calls.filter(v => v.endsWith("refresh"))).toHaveLength(1);
    expect([...f.storage.values()]).toEqual(["refresh"]);
  });
}

test("result confirmation at minute 17 restores the admitted minute-14 session and queries fresh state", async () => {
  const f = fixture(); f.advance(840000);
  const admitted = await f.session.ensureSession(f.current, 0);
  f.advance(180000);
  const current = await f.session.confirmResultSession(admitted);
  expect(current.sessionId).toBe(admitted.sessionId);
  expect(f.calls.filter(v => v.endsWith("refresh"))).toHaveLength(1);
  expect(f.calls.at(-1)).toBe("/api/auth/me");
  expect(f.calls.filter(v => v.endsWith("/me"))).toHaveLength(3);
});

for (const fault of ["prepare401", "prepare503", "slot401", "slot503", "sid", "user", "logout"]) {
  test(`fresh display gate rejects a late ${fault} change`, async () => {
    const f = fixture(); const expected = structuredClone(f.current);
    let calls = 0;
    f.onStatus(() => {
      if (++calls !== (fault.startsWith("prepare") ? 1 : 2)) return;
      if (fault.startsWith("slot") || fault.startsWith("prepare")) {
        f.document.cookie = `knowflow_active=${randomUUID()}`;
        f.fail(fault.endsWith("401") ? "me401" : "me503");
      }
      if (fault === "sid") f.current.sessionId = randomUUID();
      if (fault === "user") f.current.user.id = randomUUID();
      if (fault === "logout") f.storage.set(`knowflow.auth.pending.${expected.slot}`, "logout");
    });
    expect(await outcome(f.session.confirmResultSession(expected))).toBe(409);
    expect(f.calls.filter(v => v.endsWith("refresh"))).toHaveLength(0);
  });
}
