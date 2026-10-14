/**
 * Local tests for worker.js. No Cloudflare account or network needed:
 *
 *   node test-worker.mjs
 *
 * Exits non-zero on failure so it can be wired into CI.
 */

import worker from "./worker.js";

const BASE_ENV = { BLOCK_TERMS: "", REQUIRE_TOKEN: "", DEBUG: "false" };

async function call(method, body, headers = {}, path = "/", env = BASE_ENV) {
  const request = new Request("https://filter.example.com" + path, {
    method,
    headers: { "Content-Type": "application/json", ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const response = await worker.fetch(request, env);
  const text = await response.text();
  let json = null;
  try {
    json = JSON.parse(text);
  } catch {
    // Non-JSON responses are legitimate for OPTIONS.
  }
  return { status: response.status, json };
}

let passed = 0;
let failed = 0;

function check(name, condition, detail = "") {
  if (condition) {
    passed += 1;
    console.log(`  ok   ${name}`);
  } else {
    failed += 1;
    console.log(`  FAIL ${name} ${detail}`);
  }
}

// --- the contract the Python client depends on -------------------------
console.log("contract");
{
  const r = await call("POST", { text: "We present a sparse attention method." });
  check("normal abstract -> 200 {sensitive:false}", r.status === 200 && r.json.sensitive === false, JSON.stringify(r.json));
}
{
  const r = await call("POST", { text: "澳门赌场 六合彩 时时彩 投注平台" });
  check("gambling text -> sensitive:true", r.json.sensitive === true, JSON.stringify(r.json));
}

// --- allowlist must not discard legitimate research ---------------------
console.log("allowlist");
{
  const r = await call("POST", { text: "A study of gambling disorder treatment." });
  check("'gambling disorder' not flagged", r.json.sensitive === false);
}
{
  const r = await call("POST", { text: "赌博成瘾研究:一个临床视角" });
  check("'赌博成瘾研究' not flagged", r.json.sensitive === false);
}
{
  const r = await call("POST", { text: "GAMBLING DISORDER IN ADULTS" });
  check("allowlist is case-insensitive", r.json.sensitive === false);
}

// --- configuration ------------------------------------------------------
console.log("configuration");
{
  const r = await call("POST", { text: "这是自定义违规词" }, {}, "/", { ...BASE_ENV, BLOCK_TERMS: "自定义违规词" });
  check("BLOCK_TERMS adds a term", r.json.sensitive === true);
}
{
  const r = await call("POST", { text: "这是自定义违规词" });
  check("term not blocked when unconfigured", r.json.sensitive === false);
}

// --- auth ---------------------------------------------------------------
console.log("auth");
{
  const r = await call("POST", { text: "normal" }, {}, "/", { ...BASE_ENV, REQUIRE_TOKEN: "s3cret" });
  check("missing token -> 401", r.status === 401);
}
{
  const r = await call("POST", { text: "normal" }, { "X-Filter-Token": "s3cret" }, "/", { ...BASE_ENV, REQUIRE_TOKEN: "s3cret" });
  check("correct token -> 200", r.status === 200);
}
{
  const r = await call("POST", { text: "normal" }, { "X-Filter-Token": "wrong" }, "/", { ...BASE_ENV, REQUIRE_TOKEN: "s3cret" });
  check("wrong token -> 401", r.status === 401);
}

// --- input validation ---------------------------------------------------
console.log("validation");
{
  const r = await call("POST", { nottext: "x" });
  check("missing text -> 400", r.status === 400);
}
{
  const r = await call("POST", { text: 42 });
  check("non-string text -> 400", r.status === 400);
}
{
  const request = new Request("https://filter.example.com/", { method: "POST", body: "not-json" });
  const response = await worker.fetch(request, BASE_ENV);
  check("invalid JSON -> 400", response.status === 400);
}

// --- routing ------------------------------------------------------------
console.log("routing");
{
  const r = await call("GET", undefined, {}, "/health");
  check("GET /health -> ok", r.status === 200 && r.json.ok === true);
}
{
  const r = await call("GET", undefined, {}, "/");
  check("GET / -> 405", r.status === 405);
}
{
  const r = await call("OPTIONS", undefined, {}, "/");
  check("OPTIONS -> 204", r.status === 204);
}

// --- debug flag ---------------------------------------------------------
console.log("debug");
{
  const r = await call("POST", { text: "博彩推广" }, {}, "/", { ...BASE_ENV, DEBUG: "true" });
  check("DEBUG=true reports matched terms", Array.isArray(r.json.matched));
}
{
  const r = await call("POST", { text: "博彩推广" });
  check("DEBUG=false hides matched terms", r.json.matched === undefined);
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed === 0 ? 0 : 1);
