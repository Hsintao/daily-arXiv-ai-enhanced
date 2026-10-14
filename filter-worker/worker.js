/**
 * Content filter Worker for daily-arXiv-ai-enhanced.
 *
 * Drop-in replacement for the upstream endpoint. Same contract:
 *
 *   POST /  { "text": "..." }  ->  { "sensitive": true | false }
 *
 * The classifier is deliberately simple and self-contained: a keyword list
 * covering the categories that matter for an academic-abstract feed (spam,
 * gambling, adult, illicit goods) plus an optional set of your own terms.
 * It is NOT a general-purpose moderation system, and it is not meant to be
 * one -- the caller treats filtering as best-effort and fails open.
 *
 * Configure via wrangler.toml [vars], or override per-deployment:
 *   BLOCK_TERMS     comma-separated extra terms to treat as sensitive
 *   REQUIRE_TOKEN   when set, requests must send X-Filter-Token with this value
 *   DEBUG           when "true", include which terms matched in the response
 */

// Categories worth catching in a paper-summary feed. Kept separate so you can
// see what each group covers and delete whole groups you disagree with.
const DEFAULT_TERMS = {
  spam: [
    "加微信", "加VX", "扫码进群", "免费领取", "限时优惠", "点击链接领取",
    "代写论文", "论文代写", "快速见刊", "包过", "刷单", "返利",
    "telegram群", "博彩推广",
  ],
  gambling: [
    "赌博", "博彩", "赌场", "六合彩", "时时彩", "彩票代购", "下注",
    "投注平台", "棋牌室", "澳门赌场", "线上赌场",
  ],
  adult: [
    "色情", "成人影片", "情色", "裸聊", "约炮", "招嫖",
  ],
  illicit: [
    "枪支弹药", "代办证件", "办假证", "迷药", "毒品交易", "走私",
  ],
};

// Terms that appear in legitimate academic writing and would cause false
// positives if matched naively. Kept separate from the block list so the
// intent is obvious; add to this rather than weakening the lists above.
const ALLOWLIST = [
  "赌博成瘾研究", "gambling disorder", "problem gambling",
  "substance abuse", "药物滥用",
];

function buildTermList(env) {
  const terms = new Set();

  for (const group of Object.values(DEFAULT_TERMS)) {
    for (const term of group) terms.add(term);
  }

  const extra = (env.BLOCK_TERMS || "").split(",");
  for (const term of extra) {
    const trimmed = term.trim();
    if (trimmed) terms.add(trimmed);
  }

  return [...terms];
}

function classify(text, terms) {
  const haystack = text.toLowerCase();

  // Skip matching entirely when an allowlisted phrase is present. Simple, and
  // predictable enough for a best-effort filter.
  for (const phrase of ALLOWLIST) {
    if (haystack.includes(phrase.toLowerCase())) {
      return { sensitive: false, matched: [] };
    }
  }

  const matched = terms.filter((term) =>
    haystack.includes(term.toLowerCase())
  );
  return { sensitive: matched.length > 0, matched };
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      // Mirrors upstream: the caller is broad, and this keeps the filter
      // usable from other origins if you fork the site.
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Headers": "Content-Type, X-Filter-Token",
      "Access-Control-Allow-Methods": "POST, OPTIONS",
    },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Headers": "Content-Type, X-Filter-Token",
          "Access-Control-Allow-Methods": "POST, OPTIONS",
        },
      });
    }

    // GET /health lets you confirm a deployment without sending content.
    if (request.method === "GET" && url.pathname === "/health") {
      return json({ ok: true, service: "arxiv-content-filter" });
    }

    if (request.method !== "POST") {
      return json({ error: "Method not allowed; use POST" }, 405);
    }

    // Optional shared-secret check. Without it the endpoint is open to
    // anyone who learns the URL, which is fine for a public filter but not
    // if you care about your quota.
    if (env.REQUIRE_TOKEN) {
      const provided = request.headers.get("X-Filter-Token");
      if (provided !== env.REQUIRE_TOKEN) {
        return json({ error: "Unauthorized" }, 401);
      }
    }

    let payload;
    try {
      payload = await request.json();
    } catch {
      return json({ error: "Invalid JSON body" }, 400);
    }

    const text = payload && typeof payload.text === "string" ? payload.text : null;
    if (text === null) {
      return json({ error: "Missing 'text' string field" }, 400);
    }

    const terms = buildTermList(env);
    const result = classify(text, terms);

    const body = { sensitive: result.sensitive };
    // Term names are only echoed when DEBUG is on, so a public deployment
    // does not advertise exactly what it blocks.
    if (String(env.DEBUG).toLowerCase() === "true") {
      body.matched = result.matched;
      body.termCount = terms.length;
    }

    return json(body);
  },
};
