import assert from "node:assert/strict";
import test from "node:test";
import worker from "./index.js";

const slot = { cron: "*/10 0-3 * * *", scheduledTime: Date.parse("2026-11-27T03:00:00Z") };
const bindings = { GITHUB_TOKEN: "fixture-not-a-secret" };

function responses(t, ...results) {
  return t.mock.method(globalThis, "fetch", async () => {
    const value = results.shift();
    if (value instanceof Error) throw value;
    assert.ok(value, "No se esperaba otra llamada a GitHub");
    return new Response(null, { status: value });
  });
}

test("apertura 00:00 CLST: despacha con datos de cron y limite de tiempo", async (t) => {
  const request = responses(t, 204);
  t.mock.method(Date, "now", () => slot.scheduledTime + 1500);
  await worker.scheduled(slot, bindings);
  assert.equal(request.mock.callCount(), 1);
  const [url, options] = request.mock.calls[0].arguments;
  assert.match(url, /\/repos\/NotKn0xx\/price-watch\/dispatches$/);
  assert.equal(options.method, "POST");
  assert.equal(options.redirect, "error");
  assert.ok(options.signal instanceof AbortSignal);
  assert.deepEqual(JSON.parse(options.body), {
    event_type: "escanear",
    client_payload: {
      programado_iso: "2026-11-27T03:00:00.000Z",
      real_iso: "2026-11-27T03:00:01.500Z",
      desfase_s: 2,
      cron: slot.cron,
    },
  });
});

test("sin binding: registra una excepcion y no hace peticiones", async (t) => {
  const request = responses(t);
  await assert.rejects(worker.scheduled(slot, {}), /falta el secreto GITHUB_TOKEN/);
  assert.equal(request.mock.callCount(), 0);
});

for (const status of [401, 403, 404, 429]) {
  test(`HTTP ${status}: falla sin reintento y sin exponer credenciales`, async (t) => {
    const request = responses(t, status);
    await assert.rejects(worker.scheduled(slot, bindings), (err) => {
      assert.match(err.message, new RegExp(`HTTP ${status}`));
      assert.doesNotMatch(err.message, /fixture-not-a-secret/);
      return true;
    });
    assert.equal(request.mock.callCount(), 1);
  });
}

test("5xx transitorio: un solo reintento con el mismo payload", async (t) => {
  const request = responses(t, 503, 204);
  t.mock.method(globalThis, "setTimeout", (fn) => fn());
  await worker.scheduled(slot, bindings);
  assert.equal(request.mock.callCount(), 2);
  assert.equal(request.mock.calls[0].arguments[1].body, request.mock.calls[1].arguments[1].body);
});

test("5xx repetido: termina como error sin tercer POST", async (t) => {
  const request = responses(t, 503, 502);
  t.mock.method(globalThis, "setTimeout", (fn) => fn());
  await assert.rejects(worker.scheduled(slot, bindings), /HTTP 502/);
  assert.equal(request.mock.callCount(), 2);
});

test("corte de conexion: no duplica un POST de resultado incierto", async (t) => {
  const request = responses(t, new Error("detalle interno"));
  await assert.rejects(worker.scheduled(slot, bindings), /dispatch sin confirmacion/);
  assert.equal(request.mock.callCount(), 1);
});

test("endpoint de estado: nunca dispara una corrida", async (t) => {
  const request = responses(t);
  const value = await worker.fetch().json();
  assert.equal(value.servicio, "price-watch-cron");
  assert.equal(request.mock.callCount(), 0);
});
