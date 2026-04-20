#!/usr/bin/env python3
"""Phase A — auto-inject OpenClaw Gateway URL + x-openclaw-token on agent create.

Surgical patch to /home/paperclip/paperclip/server/src/routes/agents.ts:
  1. Add new helper `ensureOpenClawGatewayUrlAndToken` right after
     `ensureGatewayDeviceKey`.
  2. Chain the new helper inside `applyCreateDefaultsByAdapterType` so every
     create/update/effective-config path picks it up — no extra callsite
     changes needed.

Idempotent: re-running is a no-op (checks if the helper name already exists).
"""
from __future__ import annotations

import pathlib
import re
import shutil
import sys
from datetime import datetime

TARGET = pathlib.Path("/home/paperclip/paperclip/server/src/routes/agents.ts")
MARKER_FN = "ensureOpenClawGatewayUrlAndToken"

NEW_HELPER = """
  function ensureOpenClawGatewayUrlAndToken(
    adapterType: string | null | undefined,
    adapterConfig: Record<string, unknown>,
  ): Record<string, unknown> {
    // Phase-3 auto-defaults — lets every openclaw_gateway agent just work the
    // instant it's created. Without this, freshly-created gateway agents fail
    // their first run with "unauthorized: gateway token missing" because the
    // adapter has no x-openclaw-token header to send on the outbound WS
    // handshake. Source of truth for both values:
    //   OPENCLAW_GATEWAY_URL   (systemd EnvironmentFile /etc/paperclip/openclaw-gateway.env)
    //   OPENCLAW_GATEWAY_TOKEN (same file; mirrors /root/.openclaw/openclaw.json token)
    // Callers can always override by passing url/headers explicitly in
    // adapterConfig — we only fill gaps, never overwrite.
    if (adapterType !== "openclaw_gateway") return adapterConfig;
    const defaultUrl = process.env.OPENCLAW_GATEWAY_URL;
    const defaultToken = process.env.OPENCLAW_GATEWAY_TOKEN;
    const hasUrl = Boolean(asNonEmptyString(adapterConfig.url));
    const existingHeaders = (asRecord(adapterConfig.headers) ?? {}) as Record<string, unknown>;
    const hasToken =
      Boolean(asNonEmptyString(existingHeaders["x-openclaw-token"])) ||
      Boolean(asNonEmptyString(existingHeaders["x-openclaw-auth"]));
    if (hasUrl && hasToken) return adapterConfig;
    const next: Record<string, unknown> = { ...adapterConfig };
    if (!hasUrl && asNonEmptyString(defaultUrl)) {
      next.url = defaultUrl;
    }
    if (!hasToken && asNonEmptyString(defaultToken)) {
      next.headers = {
        ...existingHeaders,
        "x-openclaw-token": defaultToken,
      };
    }
    return next;
  }
"""


def main() -> int:
    if not TARGET.exists():
        print(f"ERR: {TARGET} not found", file=sys.stderr)
        return 2

    original = TARGET.read_text(encoding="utf-8")

    if MARKER_FN in original:
        print(f"[phaseA_patch] already patched (found {MARKER_FN}); skipping")
        return 0

    # 1. Backup.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = TARGET.with_suffix(f".ts.bak-{stamp}")
    shutil.copy2(TARGET, backup)
    print(f"[phaseA_patch] backup -> {backup}")

    # 2. Insert helper right after ensureGatewayDeviceKey closing brace.
    anchor_re = re.compile(
        r"(function ensureGatewayDeviceKey\([^)]*\)[^{]*\{[^}]*return adapterConfig;\s*\}\s*return \{ \.\.\.adapterConfig, devicePrivateKeyPem: generateEd25519PrivateKeyPem\(\) \};\s*\})",
        re.DOTALL,
    )
    patched = anchor_re.sub(lambda m: m.group(1) + "\n" + NEW_HELPER, original, count=1)
    if patched == original:
        # Fallback: simpler anchor — just find the closing brace of
        # ensureGatewayDeviceKey by searching for its signature and the
        # following line.
        simple_anchor = re.compile(
            r"(  function ensureGatewayDeviceKey\([\s\S]*?\n  \}\n)",
        )
        patched = simple_anchor.sub(
            lambda m: m.group(1) + NEW_HELPER, original, count=1,
        )
    if patched == original:
        print("ERR: could not locate ensureGatewayDeviceKey anchor", file=sys.stderr)
        return 3

    # 3. Chain the new helper into the two return sites that currently call
    #    ensureGatewayDeviceKey directly inside applyCreateDefaultsByAdapterType.
    #    There are three such return lines:
    #      codex_local branch, gemini_local branch, and the final catch-all.
    old = "return ensureGatewayDeviceKey(adapterType, next);"
    new = (
        "return ensureOpenClawGatewayUrlAndToken(\n"
        "        adapterType,\n"
        "        ensureGatewayDeviceKey(adapterType, next),\n"
        "      );"
    )
    before_count = patched.count(old)
    patched = patched.replace(old, new)
    after_count = patched.count(new.replace("\n", ""))
    if before_count == 0:
        print("WARN: no ensureGatewayDeviceKey callsites found — this build may "
              "already use a different pattern; aborting",
              file=sys.stderr)
        return 4
    print(f"[phaseA_patch] chained helper into {before_count} callsite(s)")

    TARGET.write_text(patched, encoding="utf-8")
    print(f"[phaseA_patch] wrote {TARGET} (+{len(patched) - len(original)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
