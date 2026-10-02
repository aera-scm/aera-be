// OAuth 2.0 access tokens for the Mirror outside SAP BTP (profile `aws`, IR-02, NFR-SEC-04).
// XSUAA is replaced by an OIDC issuer such as an Amazon Cognito user pool: AERA signs in
// with client credentials, and the Mirror accepts only that client's access tokens.
const cds = require("@sap/cds");
const { JwtVerifier } = require("aws-jwt-verify");

const SETTINGS = {
  issuer: "MIRROR_OAUTH_ISSUER",
  clientId: "MIRROR_OAUTH_CLIENT_ID",
  scope: "MIRROR_OAUTH_SCOPE",
  adminScope: "MIRROR_OAUTH_ADMIN_SCOPE",
};

function settings(options) {
  const values = {};
  for (const [name, variable] of Object.entries(SETTINGS)) {
    values[name] = String(options[name] ?? process.env[variable] ?? "").trim();
    if (!values[name]) throw new Error(`Mirror OAuth: ${variable} is required`);
  }
  let issuer;
  try {
    issuer = new URL(values.issuer);
  } catch {
    throw new Error("Mirror OAuth: the issuer must be a URL");
  }
  if (issuer.protocol !== "https:") throw new Error("Mirror OAuth: the issuer must use https");
  return values;
}

module.exports = function oauth(options = {}) {
  const { issuer, clientId, scope, adminScope } = settings(options);
  const verifier = JwtVerifier.create({
    issuer,
    audience: null, // Cognito access tokens carry client_id, not aud
    customJwtCheck: ({ payload }) => {
      if (payload.token_use !== "access") throw new Error("not an access token");
      if (payload.client_id !== clientId) throw new Error("issued to another client");
    },
  });
  // Keys given in the configuration are used as they are; otherwise the issuer's
  // /.well-known/jwks.json is fetched over https and cached.
  if (options.jwks) verifier.cacheJwks(options.jwks);

  return async function oauth(request, _response, next) {
    const bearer = /^Bearer ([^\s]+)$/i.exec(request.headers.authorization ?? "");
    if (!bearer) return next();
    let payload;
    try {
      payload = await verifier.verify(bearer[1]);
    } catch {
      return next(401);
    }
    const granted = new Set(String(payload.scope ?? "").split(" "));
    if (!granted.has(scope) && !granted.has(adminScope)) return next(401);
    cds.context.user = new cds.User({
      id: clientId,
      roles: granted.has(adminScope) ? ["MirrorAdmin"] : [],
    });
    next();
  };
};
