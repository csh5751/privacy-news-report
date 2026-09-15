const REASONS = new Set(["", "irrelevant", "duplicate", "promotional", "low_credibility", "not_interested"]);

function response(body, status, origin, env) {
  const headers = {"Content-Type":"application/json; charset=utf-8", "X-Content-Type-Options":"nosniff"};
  if (origin === env.ALLOWED_ORIGIN) { headers["Access-Control-Allow-Origin"] = origin; headers["Vary"] = "Origin"; }
  return new Response(JSON.stringify(body), {status, headers});
}
const text = (value, max) => typeof value === "string" ? value.trim().slice(0, max) : "";

export default { async fetch(request, env) {
  const url = new URL(request.url), origin = request.headers.get("Origin") || "";
  if (request.method === "OPTIONS") {
    if (origin !== env.ALLOWED_ORIGIN) return response({error:"origin_not_allowed"}, 403, origin, env);
    return new Response(null, {status:204, headers:{"Access-Control-Allow-Origin":origin,"Access-Control-Allow-Methods":"GET, POST, OPTIONS","Access-Control-Allow-Headers":"Content-Type","Access-Control-Max-Age":"86400","Vary":"Origin"}});
  }
  if (request.method === "GET" && url.pathname === "/health") return response({ok:true}, 200, origin, env);
  if (request.method === "GET" && url.pathname === "/signals") {
    const result = await env.DB.prepare(`SELECT a.article_id,a.title,a.source,a.topic,a.url,f.vote,f.reason,COUNT(*) AS count FROM feedback_votes f JOIN articles a ON a.article_id=f.article_id GROUP BY a.article_id,f.vote,f.reason ORDER BY MAX(f.updated_at) DESC LIMIT 2000`).all();
    return response({signals:result.results}, 200, origin, env);
  }
  if (request.method !== "POST" || url.pathname !== "/feedback") return response({error:"not_found"}, 404, origin, env);
  if (origin !== env.ALLOWED_ORIGIN) return response({error:"origin_not_allowed"}, 403, origin, env);
  if (Number(request.headers.get("Content-Length") || 0) > 10000) return response({error:"too_large"}, 413, origin, env);
  let data; try { data = await request.json(); } catch { return response({error:"invalid_json"}, 400, origin, env); }
  const articleId=text(data.article_id,64), voterId=text(data.voter_id,64), vote=text(data.vote,8), reason=text(data.reason,32);
  const title=text(data.title,300), source=text(data.source,100), topic=text(data.topic,100), articleUrl=text(data.url,1000);
  if (!/^[a-f0-9]{24}$/.test(articleId) || !/^[a-f0-9-]{20,64}$/i.test(voterId) || !["up","down"].includes(vote) || !REASONS.has(reason) || !title || !articleUrl) return response({error:"invalid_fields"}, 400, origin, env);
  await env.DB.batch([
    env.DB.prepare(`INSERT INTO articles(article_id,title,source,topic,url,updated_at) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(article_id) DO UPDATE SET title=excluded.title,source=excluded.source,topic=excluded.topic,url=excluded.url,updated_at=CURRENT_TIMESTAMP`).bind(articleId,title,source,topic,articleUrl),
    env.DB.prepare(`INSERT INTO feedback_votes(article_id,voter_id,vote,reason,updated_at) VALUES(?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(article_id,voter_id) DO UPDATE SET vote=excluded.vote,reason=excluded.reason,updated_at=CURRENT_TIMESTAMP`).bind(articleId,voterId,vote,reason)
  ]);
  return response({ok:true}, 200, origin, env);
}};
