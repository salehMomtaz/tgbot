# utils/pot_provider.py
"""Async lifecycle manager for the bgutil-ytdlp-pot-provider Deno server.

The provider generates YouTube proof-of-origin (PO) tokens. It runs on the Deno
runtime (no Node.js/npm/tsc). This manager:

  * verifies Deno >= 2.0 is available,
  * installs the server's npm deps (incl. the native `canvas` FFI build),
  * patches `server/src/main.ts` so it binds 127.0.0.1 only (the upstream server
    binds the wildcard `::`/`0.0.0.0`; on a public VPS we never want the PO-token
    endpoint reachable from the internet),
  * launches `deno run ... src/main.ts --port <port>` and waits for `/ping`,
  * supervises it with a health-check + backoff restart loop.

The yt-dlp plugin that actually talks to this server is pip-installed
(`bgutil-ytdlp-pot-provider` in requirements.txt) and auto-discovered by yt-dlp,
so there is no symlink hack here.
"""

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path

import config

logger = logging.getLogger(__name__)

# Marker injected into server/src/main.ts to make the localhost patch idempotent.
_LOCALHOST_MARKER = "// TGBOT_LOCALHOST_PATCH"


class PotProviderManager:
    """Installs, patches, starts, monitors, and stops the bgutil POT Deno server."""

    def __init__(
        self,
        provider_path: str | None = None,
        port: int | None = None,
        deno_bin: str | None = None,
        provider_ref: str | None = None,
    ):
        self.provider_path = Path(provider_path or config.YTDLP_POT_PROVIDER_PATH).resolve()
        self.port = port or config.YTDLP_POT_PORT
        self.deno_bin = deno_bin or getattr(config, "YTDLP_POT_DENO_BIN", "deno")
        # Defensive fallback: if the configured Deno binary isn't on PATH (e.g. the
        # bot was launched by `python main.py` in a shell that hasn't picked up
        # install.sh's ~/.bashrc PATH export), use install.sh's default location.
        # This keeps the provider working for newbies who don't launch via run.sh.
        if not shutil.which(self.deno_bin):
            fallback = Path(os.path.expanduser("~/.deno/bin/deno"))
            if fallback.exists():
                self.deno_bin = str(fallback)
        self.provider_ref = provider_ref or getattr(config, "YTDLP_POT_PROVIDER_REF", "1.3.1")
        self.main_ts = self.provider_path / "src" / "main.ts"
        self.node_modules = self.provider_path / "node_modules"
        self.proc: asyncio.subprocess.Process | None = None
        self._last_health_ok = False
        self._consecutive_failures = 0
        self._stdout_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Deno / install helpers
    # ------------------------------------------------------------------
    def _deno_version_ok(self) -> tuple[bool, str | None]:
        """Return (ok, version). Deno >= 2.0 is required."""
        try:
            if not shutil.which(self.deno_bin):
                return False, None
            output = os.popen(f"{self.deno_bin} --version 2>/dev/null").read().strip()
            if not output:
                return False, None
            first_line = output.splitlines()[0]
            # First line looks like "deno 2.1.3" — pick the numeric token.
            version = next(
                (tok for tok in first_line.split() if tok[:1].isdigit()), None
            )
            if not version:
                return False, first_line
            major = int(version.split(".")[0])
            return (major >= 2), version
        except Exception:
            return False, None

    async def _run_deno(self, *args: str, cwd: Path) -> None:
        """Run a deno command (install/build helper) and raise on failure."""
        cmd = [self.deno_bin, *args]
        logger.info(f"[POT] Running: {' '.join(cmd)} in {cwd}")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"deno {' '.join(args)} failed (exit {proc.returncode}):\n"
                f"{stdout.decode(errors='replace')}\n{stderr.decode(errors='replace')}"
            )
        logger.info(f"[POT] deno {' '.join(args)} completed")

    async def _ensure_installed(self) -> None:
        """Verify Deno + the provider source, and populate node_modules if missing."""
        if not self.main_ts.exists():
            raise RuntimeError(
                f"bgutil provider not found at {self.main_ts}. "
                f"Run ./install.sh, or manually clone:\n"
                f"  git clone --single-branch --branch {self.provider_ref} "
                "https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git bgutil-provider"
            )

        ok, version = self._deno_version_ok()
        if not ok:
            raise RuntimeError(
                f"Deno >= 2.0 is required for the PO-token provider (found: {version}). "
                "Install it with: curl -fsSL https://deno.land/install.sh | sh"
            )

        if not self.node_modules.exists():
            # Prefer the committed lockfile (--frozen); fall back without it if the
            # lock is missing or out of date on this ref. `--allow-scripts` (no
            # package list) runs every npm lifecycle script so transitive deps
            # (e.g. @swc/core) are not left "ignored" with an un-actionable
            # warning — the provider is a pinned, trusted ref.
            try:
                await self._run_deno(
                    "install", "--allow-scripts", "--frozen", cwd=self.provider_path
                )
            except RuntimeError as exc:
                logger.warning(f"[POT] 'deno install --frozen' failed; retrying without --frozen: {exc}")
                await self._run_deno(
                    "install", "--allow-scripts", cwd=self.provider_path
                )

        self._patch_localhost()

    def _patch_localhost(self) -> None:
        """Patch server/src/main.ts to bind 127.0.0.1 only. Idempotent.

        Upstream binds the wildcard ``::`` and falls back to ``0.0.0.0``; on a
        public VPS that exposes the PO-token endpoint to the internet. We rewrite
        both listen calls to loopback. If the upstream source changed shape so the
        markers no longer match, we log a security warning rather than stay quiet.
        """
        if not self.main_ts.exists():
            return
        content = self.main_ts.read_text(encoding="utf-8")
        if _LOCALHOST_MARKER in content:
            return

        patched = content
        patched = patched.replace('host: "::",', f'host: "127.0.0.1", {_LOCALHOST_MARKER}')
        patched = patched.replace('host: "0.0.0.0",', f'host: "127.0.0.1", {_LOCALHOST_MARKER}')

        if patched == content:
            logger.warning(
                "[POT] Could not patch src/main.ts for localhost binding (upstream changed?). "
                "The provider may bind all network interfaces — verify firewall/exposure."
            )
            return

        self.main_ts.write_text(patched, encoding="utf-8")
        logger.info(f"[POT] Patched {self.main_ts} to bind 127.0.0.1 only")

    # ------------------------------------------------------------------
    # Process lifecycle
    # ------------------------------------------------------------------
    async def _pipe_logger(self, stream: asyncio.StreamReader | None, prefix: str) -> None:
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                logger.info(f"{prefix} {text}")

    async def start(self) -> None:
        if self._running:
            return

        await self._ensure_installed()

        cmd = [
            self.deno_bin,
            "run",
            "--allow-env",
            "--allow-net",
            "--allow-ffi",
            "--allow-read",
            "--allow-sys",
            "src/main.ts",
            "--port",
            str(self.port),
        ]
        logger.info(f"[POT] Starting server: {' '.join(cmd)}")
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.provider_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._running = True

        self._stdout_task = asyncio.create_task(
            self._pipe_logger(self.proc.stdout, "[POT stdout]")
        )
        self._stderr_task = asyncio.create_task(
            self._pipe_logger(self.proc.stderr, "[POT stderr]")
        )

        # Deno's first run compiles + canvas FFI init can be slow on a weak VPS,
        # so wait up to ~30s for the health endpoint to respond.
        for _ in range(60):
            await asyncio.sleep(0.5)
            if self.proc.returncode is not None:
                self._set_available(False)
                raise RuntimeError(
                    f"PO-token provider exited early with code {self.proc.returncode}"
                )
            try:
                if await self._ping():
                    self._last_health_ok = True
                    self._consecutive_failures = 0
                    self._set_available(True)
                    logger.info(f"[POT] Provider is healthy on 127.0.0.1:{self.port}")
                    return
            except Exception:
                pass

        self._set_available(False)
        raise RuntimeError("PO-token provider did not become healthy within 30 seconds")

    def _set_available(self, flag: bool) -> None:
        """Mirror provider health into utils.shared.POT_AVAILABLE (lazy import)."""
        try:
            import utils.shared as shared
            shared.POT_AVAILABLE = bool(flag)
        except Exception:
            pass

    async def stop(self) -> None:
        self._running = False
        self._set_available(False)
        if self.proc is None:
            return

        try:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                logger.warning("[POT] Provider did not terminate gracefully, killing it")
                self.proc.kill()
                await self.proc.wait()
        except ProcessLookupError:
            pass
        finally:
            self.proc = None
            for task in (self._stdout_task, self._stderr_task):
                if task and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            self._stdout_task = None
            self._stderr_task = None
            logger.info("[POT] Provider stopped cleanly")

    # ------------------------------------------------------------------
    # Health monitoring
    # ------------------------------------------------------------------
    async def _ping(self) -> bool:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        try:
            request = (
                f"GET /ping HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                f"Connection: close\r\n\r\n"
            )
            writer.write(request.encode())
            await writer.drain()

            response = b""
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                response += chunk

            if b"200 OK" not in response:
                return False
            body = response.split(b"\r\n\r\n", 1)[-1]
            data = json.loads(body.decode("utf-8", errors="replace"))
            return isinstance(data.get("server_uptime"), (int, float))
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def health_check_loop(self) -> None:
        """Run forever while the bot is up; restart the provider if it dies."""
        while self._running:
            await asyncio.sleep(30)
            if not self._running:
                break

            if self.proc is None or self.proc.returncode is not None:
                rc = getattr(self.proc, "returncode", None) if self.proc else None
                logger.warning(f"[POT] Provider process is gone (returncode={rc}); will restart (failures={self._consecutive_failures+1})")
                self._consecutive_failures += 1
            else:
                try:
                    ok = await self._ping()
                except Exception as exc:
                    logger.debug(f"[POT] Health ping failed: {exc}")
                    ok = False

                if ok:
                    self._last_health_ok = True
                    self._consecutive_failures = 0
                    self._set_available(True)
                    continue

                self._consecutive_failures += 1
                logger.warning(
                    f"[POT] Provider health check failed "
                    f"({self._consecutive_failures} times)"
                )

            # Restart with backoff
            try:
                await self.stop()
            except Exception as exc:
                logger.warning(f"[POT] Error stopping provider for restart: {exc}")

            backoff = min(60, 5 * (2 ** (self._consecutive_failures - 1)))
            logger.info(f"[POT] Restarting provider in {backoff}s...")
            await asyncio.sleep(backoff)

            try:
                await self.start()
            except Exception as exc:
                logger.error(f"[POT] Failed to restart provider: {exc}")

    def is_running(self) -> bool:
        if not self._running or self.proc is None:
            return False
        if self.proc.returncode is not None:
            return False
        return self._last_health_ok
