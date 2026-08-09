// xchat_bridge.mjs — XChat (encrypted self-DM) reader for the tgbot direct-forward relay.
//
// Runs under Deno (node >= 20 is required by emusks' x-client-transaction-id, but the
// box ships node 18; Deno 2.x runs the same npm package fine). It logs into X with the
// shared xcookies jar, recovers the account's XChat identity from the Juicebox realms
// with the operator's PIN, then polls ONLY the self-DM conversation. Every NEW message
// (seq > persisted cursor) is decoded — XChat-encrypted via the conversation key, or
// plaintext thrift for legacy-sent messages — and appended as one canonical JSON line to
// cache/xchat_inbox.jsonl. The Python bot polls that file and relays via yt-dlp/pyrogram.
//
// Cursor semantics: first boot primes `last_seq` to the newest message (backlog is
// skipped); later boots resume from the persisted `last_seq`, so messages that arrived
// while the bridge was down are still emitted. The bot keeps its OWN cursor in
// direct_forward_state.json (same id space — the XChat sequenceId equals the legacy DM id),
// so nothing is double-relayed.
//
// Canonical line schema (one JSON object per line):
//   {"id":"<seq>","at":"<ms>","kind":"tweet","url":"https://x.com/i/status/<id>","text":""}
//   {"id":"<seq>","at":"<ms>","kind":"media","media_url":"https://ton.x.com/...","is_photo":true,"text":"..."}
//   {"id":"<seq>","at":"<ms>","kind":"text","text":"hello"}
// Non-relayable events (reactions, deletes, edits, read receipts, key changes) are skipped.

import Emusks from "./node_modules/emusks/src/index.js";
import {
  parseThrift,
  thriftStr,
  b64decode,
  eciesUnwrap,
  decryptBody,
} from "./node_modules/emusks/src/helpers/xchat-crypto.js";
import { appendFileSync, existsSync, readFileSync, writeFileSync, mkdirSync } from "node:fs";

const JAR = Deno.env.get("X_COOKIES") || "cookies/twitter/xcookies.txt";
const PIN = Deno.env.get("XCHAT_PIN") || "";
// Poll on a RANDOM window every cycle (uniform 10 s .. 10 min) — never a fixed
// cadence. Fixed short polling is the fingerprint that got the IG account
// flagged; X tolerates it better but the same hygiene applies. The window is
// re-rolled on every cycle, so consecutive gaps are never equal.
const POLL_MIN_S = Math.max(10, Number(Deno.env.get("XCHAT_POLL_MIN_SECONDS") || 10));
const POLL_MAX_S = Math.min(600, Math.max(POLL_MIN_S, Number(Deno.env.get("XCHAT_POLL_MAX_SECONDS") || 600)));
const nextPollMs = () => (POLL_MIN_S + Math.random() * (POLL_MAX_S - POLL_MIN_S)) * 1000;
const INBOX = Deno.env.get("XCHAT_INBOX") || "cache/xchat_inbox.jsonl";
const STATE = Deno.env.get("XCHAT_STATE") || "cache/xchat_bridge_state.json";

if (!PIN) {
  console.error("[xchat_bridge] XCHAT_PIN is not set — cannot recover the XChat identity.");
  Deno.exit(2);
}

function readJarCookies(path) {
  const out = {};
  let text;
  try {
    text = readFileSync(path, "utf8");
  } catch {
    console.error(`[xchat_bridge] cannot read jar ${path}`);
    return out;
  }
  for (const line of text.split("\n")) {
    if (line.startsWith("#") || !line.trim()) continue;
    const f = line.split("\t");
    if (f.length < 7) continue;
    if (f[0].includes("x.com") || f[0].includes("twitter.com")) out[f[5]] = f[6];
  }
  return out;
}

function loadState() {
  try {
    if (existsSync(STATE)) return JSON.parse(readFileSync(STATE, "utf8"));
  } catch {}
  return {};
}

function saveState(state) {
  mkdirSync("cache", { recursive: true });
  writeFileSync(STATE, JSON.stringify(state));
}

// Decode a MessageEntryHolder byte blob (the plaintext of a message create event).
function decodeContents(holderBytes) {
  const holder = parseThrift(holderBytes);
  const entry = holder[1] ?? {};
  if (!entry[1]) return { kind: "other" };
  const c = entry[1];
  const out = { kind: "message", text: thriftStr(c[1]) ?? "" };
  if (c[3]) {
    out.attachments = c[3].map((a) => {
      const variant = Object.keys(a)[0];
      return { variant, data: a[variant] };
    });
  }
  return out;
}

// Map a decoded message into the canonical line (or null when not relayable).
function canonicalize(msg, seq, at) {
  if (msg.kind !== "message") return null;
  const atts = msg.attachments ?? [];
  const post = atts.find((a) => a.variant === "2");
  if (post) {
    const url = thriftStr(post.data?.[2]);
    if (url && url.length > 0) {
      return { id: seq, at, kind: "tweet", url, text: msg.text || "" };
    }
  }
  const media = atts.find((a) => a.variant === "1");
  if (media) {
    const mediaUrl = thriftStr(media.data?.[8]);
    const type = media.data?.[3];
    if (mediaUrl && mediaUrl.length > 0) {
      return {
        id: seq, at, kind: "media",
        media_url: mediaUrl,
        is_photo: type === 1,
        text: msg.text || "",
      };
    }
    return {
      id: seq, at, kind: "media",
      media_url: "",
      media_hash_key: thriftStr(media.data?.[1]) ?? null,
      media_type: typeof type === "number" ? type : null,
      encrypted_media: true,
      text: msg.text || "",
    };
  }
  const urlAtt = atts.find((a) => a.variant === "3");
  if (urlAtt && msg.text) return { id: seq, at, kind: "text", text: msg.text };
  if (msg.text && msg.text.trim().length > 0) return { id: seq, at, kind: "text", text: msg.text };
  return null;
}

async function buildCKeyMap(keyChangeB64s, myUserId, identityKey) {
  const map = {};
  for (const b64v of keyChangeB64s ?? []) {
    try {
      const cke = parseThrift(b64decode(b64v))[7]?.[3];
      if (!cke) continue;
      const version = thriftStr(cke[1]);
      for (const p of cke[2] ?? []) {
        if (thriftStr(p[1]) === String(myUserId)) {
          map[version] = await eciesUnwrap(thriftStr(p[2]), identityKey);
        }
      }
    } catch {}
  }
  return map;
}

// Fetch and decode every message event with seq > `since` from the self-conversation,
// newest-first. Returns decoded canonical lines in ascending seq order.
async function fetchNewMessages(client, convId, since, id) {
  const lines = [];
  let before = "9223372036854775807";
  for (let pg = 0; pg < 40; pg++) {
    const res = await client.xchat.gql("GetConversationPageQuery", {
      conversation_id: convId,
      min_local_sequence_id: before,
      min_conversation_key_version: "9223372036854775807",
    });
    const page = res?.data?.get_conversation_page ?? {};
    const events = page.encoded_message_events ?? [];
    if (!events.length) break;
    const cKeyMap = await buildCKeyMap(page.missing_conversation_key_change_events, id.userId, id.identityKey);
    let oldest = before;
    for (const b64v of events) {
      const ev = parseThrift(b64decode(b64v));
      const seq = thriftStr(ev[1]);
      if (!seq) continue;
      if (BigInt(seq) < BigInt(oldest)) oldest = seq;
      if (BigInt(seq) <= BigInt(since)) continue;
      const mce = ev[7]?.[1];
      if (!mce) continue;
      let msg;
      const cKeyVer = thriftStr(mce[101]);
      if (cKeyVer) {
        const cKey = cKeyMap[cKeyVer];
        if (!cKey) continue;
        const plain = decryptBody(mce[100], cKey);
        if (!plain) continue;
        msg = decodeContents(plain);
      } else {
        msg = decodeContents(mce[100]);
      }
      const line = canonicalize(msg, seq, thriftStr(ev[6]));
      if (line) lines.push(line);
    }
    if (!page.has_more || BigInt(oldest) <= BigInt(since)) break;
    before = String(BigInt(oldest) - 1n);
  }
  lines.sort((a, b) => (BigInt(a.id) < BigInt(b.id) ? -1 : 1));
  return lines;
}

async function main() {
  const cookies = readJarCookies(JAR);
  const authToken = cookies.auth_token;
  if (!authToken) {
    console.error("[xchat_bridge] no auth_token in the xcookies jar — X direct-forward bridge disabled.");
    Deno.exit(2);
  }

  const client = new Emusks();
  await client.login({ auth_token: authToken });
  const me = await client.account.viewer();
  if (!me?.id) {
    console.error("[xchat_bridge] login succeeded but no viewer id.");
    Deno.exit(2);
  }

  await client.xchat.recover(PIN);
  console.log(`[xchat_bridge] logged in as @${me.username} (${me.id}); XChat identity recovered.`);

  const state = loadState();
  let since = state.last_seq;
  const convId = `${me.id}:${me.id}`;

  const { messages } = await client.xchat.read(me.id);
  const newest = messages.length ? String(messages[0].sequenceId) : "0";
  if (!since) {
    since = newest;
    saveState({ last_seq: since });
    console.log(`[xchat_bridge] first run — cursor primed to ${since}, backlog skipped.`);
  }
  console.log(`[xchat_bridge] polling self-DM ${convId} on a random ${POLL_MIN_S}-${POLL_MAX_S}s window from seq ${since}`);

  while (true) {
    try {
      const fresh = await fetchNewMessages(client, convId, since, client._xchat);
      if (fresh.length) {
        const tmp = `${INBOX}.tmp`;
        for (const line of fresh) {
          appendFileSync(tmp, JSON.stringify(line) + "\n");
        }
        try {
          appendFileSync(INBOX, readFileSync(tmp, "utf8"));
        } finally {
          try { Deno.remove(tmp); } catch {}
        }
        since = fresh[fresh.length - 1].id;
        saveState({ last_seq: since });
        console.log(`[xchat_bridge] emitted ${fresh.length} new message(s) up to ${since}`);
      }
    } catch (e) {
      console.error(`[xchat_bridge] poll error: ${e?.message ?? e}`);
    }
    await new Promise((r) => setTimeout(r, nextPollMs()));
  }
}

try {
  await main();
} catch (e) {
  console.error(`[xchat_bridge] fatal: ${e?.stack ?? e}`);
  Deno.exit(1);
}
