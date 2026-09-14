import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const api = readFileSync(new URL("../src/lib/api.ts", import.meta.url), "utf8");
const context = readFileSync(new URL("../src/context/DeskContext.tsx", import.meta.url), "utf8");

test("desk reads have a bounded timeout", () => {
  assert.match(api, /const READ_TIMEOUT_MS = 8000/);
  assert.match(api, /controller\.abort\(\)/);
  assert.match(api, /throw new Error\("request_timeout"\)/);
});

test("initial light and broker reads do not wait on each other", () => {
  assert.match(context, /Promise\.allSettled\(\[\s*refreshLight\(lightAbort\.signal\),\s*refreshBroker\(false\)/);
});

test("broker data can render before the light desk arrives", () => {
  assert.match(api, /if \(!light && !broker\) return null/);
  assert.match(api, /light_available: light !== null/);
});
