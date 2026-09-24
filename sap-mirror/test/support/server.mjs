// Starts the real Mirror server (the same entry point as production) on a free port and
// offers a small HTTP client. Tests talk to it over HTTP exactly as AERA does.
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { join } from "node:path";

const root = join(import.meta.dirname, "..", "..");
const require = createRequire(join(root, "package.json"));

export async function startMirror(env = {}) {
  const serve = require.resolve("@sap/cds/bin/serve.js");
  const child = spawn(process.execPath, [serve], {
    cwd: root,
    env: { ...process.env, PORT: "0", NODE_ENV: "development", ...env },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let log = "";
  const url = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`Mirror did not start:\n${log}`)), 60_000);
    const onData = (chunk) => {
      log += chunk;
      const match = /server listening on \{ url: '([^']+)'/.exec(log);
      if (match) {
        clearTimeout(timer);
        resolve(match[1]);
      }
    };
    child.stdout.on("data", onData);
    child.stderr.on("data", onData);
    child.on("exit", (code) => reject(new Error(`Mirror exited with ${code}:\n${log}`)));
  });
  return {
    url,
    log: () => log,
    stop: () =>
      new Promise((resolve) => {
        child.removeAllListeners("exit");
        child.once("exit", resolve);
        child.kill();
      }),
  };
}

export function client(base) {
  async function call(method, path, body, headers = {}) {
    const response = await fetch(base + path, {
      method,
      headers: {
        accept: "application/json",
        ...(body === undefined ? {} : { "content-type": "application/json" }),
        ...headers,
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await response.text();
    let data = text;
    try {
      data = text ? JSON.parse(text) : undefined;
    } catch {
      // $metadata and plain-text errors stay text.
    }
    return {
      status: response.status,
      header: (name) => response.headers.get(name),
      cookies: () => response.headers.getSetCookie().map((c) => c.split(";")[0]).join("; "),
      data,
    };
  }
  return {
    get: (path, headers) => call("GET", path, undefined, headers),
    post: (path, body, headers) => call("POST", path, body, headers),
    patch: (path, body, headers) => call("PATCH", path, body, headers),
    delete: (path, headers) => call("DELETE", path, undefined, headers),
  };
}
