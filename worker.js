/**
 * LINE Webhook（Cloudflare Workers 免費方案）
 *
 * 用途：取得 userId / groupId，填入 GitHub Secret「LINE_TO」。
 *       在與 Bot 的聊天或群組中輸入 /id，Bot 會回覆該聊天的 ID。
 *       雨量警戒是由 GitHub Actions 主動推播，不經過這支 Webhook。
 *
 * 需在 Worker 的 Settings → Variables and Secrets 設定（類型選 Secret）：
 *   LINE_CHANNEL_SECRET        驗證請求確實來自 LINE
 *   LINE_CHANNEL_ACCESS_TOKEN  用來回覆訊息
 *   ADMIN_USER_ID（選填）      設定後只有這個 userId 下的 /id 會被回應
 */
export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") return new Response("OK");

    const body = await request.text();
    const signature = request.headers.get("x-line-signature") || "";
    if (!(await verifySignature(body, signature, env.LINE_CHANNEL_SECRET))) {
      return new Response("Invalid signature", { status: 401 });
    }

    let events = [];
    try {
      events = JSON.parse(body).events || [];
    } catch {
      return new Response("Bad request", { status: 400 });
    }
    ctx.waitUntil(Promise.all(events.map((e) => handleEvent(e, env))));
    return new Response("OK"); // LINE 要求盡快回 200
  },
};

// 以 Channel secret 對原始 body 做 HMAC-SHA256，與 X-Line-Signature 比對（常數時間）
async function verifySignature(body, signature, secret) {
  if (!secret || !signature) return false;
  try {
    const enc = new TextEncoder();
    const key = await crypto.subtle.importKey(
      "raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["verify"]
    );
    const sig = Uint8Array.from(atob(signature), (c) => c.charCodeAt(0));
    return await crypto.subtle.verify("HMAC", key, sig, enc.encode(body));
  } catch {
    return false;
  }
}

async function handleEvent(event, env) {
  if (event.type !== "message" || event.message?.type !== "text") return;
  if (event.message.text.trim().toLowerCase() !== "/id") return;

  const src = event.source || {};
  if (env.ADMIN_USER_ID && src.userId !== env.ADMIN_USER_ID) return;

  const lines = [];
  if (src.groupId) lines.push(`groupId：${src.groupId}`);
  if (src.roomId) lines.push(`roomId：${src.roomId}`);
  if (src.userId) lines.push(`userId：${src.userId}`);

  await fetch("https://api.line.me/v2/bot/message/reply", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${env.LINE_CHANNEL_ACCESS_TOKEN}`,
    },
    body: JSON.stringify({
      replyToken: event.replyToken,
      messages: [{ type: "text", text: lines.join("\n") || "取不到 ID" }],
    }),
  });
}
