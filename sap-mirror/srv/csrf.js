// CSRF token handling as an SAP Gateway does it (IR-02): a GET with `X-CSRF-Token: Fetch`
// returns a token bound to a session cookie; every modifying request must send both.
const crypto = require("node:crypto");

const COOKIE = "SAP_SESSIONID_MIRROR";
const SAFE = new Set(["GET", "HEAD", "OPTIONS"]);

function sessionOf(request) {
  const header = request.headers.cookie ?? "";
  for (const part of header.split(";")) {
    const [name, ...value] = part.trim().split("=");
    if (name === COOKIE) return value.join("=");
  }
  return undefined;
}

function csrf({ secret = crypto.randomBytes(32) } = {}) {
  const tokenFor = (session) =>
    crypto.createHmac("sha256", secret).update(session).digest("base64url");

  return function csrfMiddleware(request, response, next) {
    const asked = String(request.headers["x-csrf-token"] ?? "").toLowerCase();
    if (SAFE.has(request.method)) {
      if (asked === "fetch") {
        let session = sessionOf(request);
        if (!session) {
          session = crypto.randomBytes(24).toString("base64url");
          response.append("Set-Cookie", `${COOKIE}=${session}; Path=/; HttpOnly; SameSite=Strict`);
        }
        response.set("x-csrf-token", tokenFor(session));
      }
      return next();
    }
    const session = sessionOf(request);
    const sent = request.headers["x-csrf-token"];
    const valid =
      session &&
      typeof sent === "string" &&
      sent.length > 0 &&
      crypto.timingSafeEqual(
        crypto.createHash("sha256").update(sent).digest(),
        crypto.createHash("sha256").update(tokenFor(session)).digest(),
      );
    if (!valid) {
      response.set("x-csrf-token", "Required");
      return response.status(403).send("CSRF token validation failed");
    }
    return next();
  };
}

module.exports = { csrf, COOKIE };
