import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const source = readFileSync(new URL("../src/components/desk/orbSignalStatus.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext } });
const { isPotentialOrbState, orbSignalStatus, observationTime } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);

test("shows the observed phase without inferring progress from price", () => {
  assert.equal(orbSignalStatus("WAIT", ["ORB_RETEST_WAIT_BREAKOUT"]), "1/3 · Ждём пробой");
  assert.equal(orbSignalStatus("WAIT", ["ORB_RETEST_WAIT_RETURN"]), "2/3 · Ждём возврат");
  assert.equal(orbSignalStatus("WAIT", ["ORB_RETEST_WAIT_CONFIRMATION"]), "3/3 · Ждём подтверждение");
});
test("missing or blocked facts never imply a ready signal", () => {
  assert.equal(orbSignalStatus("DATA_BLOCKED", ["ORB_RETEST_CONFIRMED"]), "Нет данных для проверки");
  assert.equal(orbSignalStatus("BLOCKED", ["ORB_RETEST_CONFIRMED"]), "Вход заблокирован");
  assert.equal(orbSignalStatus(undefined, []), "Проверяем условия сигнала");
  assert.equal(orbSignalStatus("WAIT", ["UNKNOWN"]), "Проверяем условия сигнала");
});
test("expired signals and price waits remain distinct", () => {
  assert.equal(orbSignalStatus("WAIT", ["ORB_RETEST_EXPIRED"]), "Сигнал истёк · ждём новый");
  assert.equal(orbSignalStatus("WAIT", ["ORB_WAITING_PULLBACK"]), "Ждём цену в зоне покупки");
});
test("cards contain only genuine potential entries", () => {
  assert.equal(isPotentialOrbState("DATA_BLOCKED", ["ORB_RETEST_WAIT_RETURN"]), false);
  assert.equal(isPotentialOrbState("WAIT", ["ORB_RETEST_WAIT_BREAKOUT"]), false);
  assert.equal(isPotentialOrbState("WAIT", ["ORB_RETEST_WAIT_RETURN"]), true);
  assert.equal(isPotentialOrbState("WAIT", ["ORB_RETEST_WAIT_CONFIRMATION"]), true);
  assert.equal(isPotentialOrbState("WAIT", [], true), true);
  assert.equal(isPotentialOrbState("WAIT", [], false, "CHECKING"), true);
  assert.equal(isPotentialOrbState("WAIT", [], false, "EXPIRED"), false);
});
test("observation time uses ET and does not fabricate missing timestamps", () => {
  assert.equal(observationTime("2026-09-11T13:43:12Z"), "09:43:12");
  assert.equal(observationTime("invalid"), null);
  assert.equal(observationTime(), null);
});
