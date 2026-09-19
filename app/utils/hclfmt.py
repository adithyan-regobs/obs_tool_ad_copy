"""
HCL formatter wrapper.

Pipes a string of HCL2 (Terraform / Terragrunt) through whichever HCL
formatter is on PATH and returns the formatted output. Falls back to the
input content unchanged when no formatter is installed or formatting
fails — never raises.

Lookup order:
1. `hclfmt`        — small standalone binary from `hashicorp/hcl` (MPL-2.0).
2. `terraform fmt` — bundled with the Terraform CLI (BSL post-2023).

Both produce identical output for our use case; they call the same HCL2
formatter library internally. Resolution is cached at module load — if
the runtime environment changes mid-process, restart obs_tool to re-detect.
"""

import asyncio
import logging
import shutil
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

_FMT_TIMEOUT_SECONDS = 10.0


def hcl_escape(value) -> str:
    """
    Escape a value for interpolation inside an HCL2 double-quoted string.

    Anything user-supplied that ends up between quotes in generated HCL must
    go through this first. Without it a value can close the string early and
    inject sibling attributes, e.g.

        partition_key = 'pk", deletion_protection = false, x = "'

    renders as three attributes instead of one — valid HCL, so it commits and
    applies with nothing to catch it.

    Handles the four sequences HCL treats specially inside quotes:
      \\  escape character      "  string terminator
      ${  interpolation         %{  template directive

    Backslash must be replaced first, otherwise it re-escapes the backslashes
    introduced by the later replacements.

    Note this is NOT sufficient on its own when the result is passed to
    re.sub() as a replacement string — that argument is parsed for group
    references (\\1, \\g<name>), so pass a callable replacement as well.
    """
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("${", "$${")
        .replace("%{", "%%{")
    )


def _resolve_formatter() -> Tuple[Optional[str], List[str]]:
    """Pick the first available HCL formatter binary on PATH."""
    if shutil.which("hclfmt"):
        return "hclfmt", []
    if shutil.which("terraform"):
        return "terraform", ["fmt", "-"]
    return None, []


_BINARY, _ARGS = _resolve_formatter()
if _BINARY is None:
    logger.info(
        "hclfmt: no HCL formatter on PATH (tried hclfmt, terraform); "
        "rendered HCL will be staged unformatted."
    )
else:
    logger.info("hclfmt: using %s for HCL formatting", _BINARY)


async def hclfmt(content: str, *, timeout: float = _FMT_TIMEOUT_SECONDS) -> str:
    """
    Format an HCL string. Returns the formatted text on success, or the
    original content unchanged on any failure (binary missing, timeout,
    non-zero exit, OSError).
    """
    if _BINARY is None or not content:
        return content

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            _BINARY, *_ARGS,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(content.encode("utf-8")),
            timeout=timeout,
        )
        if proc.returncode == 0:
            return stdout.decode("utf-8")
        logger.warning(
            "hclfmt: %s exited %s; stderr=%s; returning unformatted",
            _BINARY,
            proc.returncode,
            stderr.decode("utf-8", errors="replace")[:500],
        )
    except asyncio.TimeoutError:
        logger.warning("hclfmt: %s timed out after %ss; returning unformatted", _BINARY, timeout)
        if proc is not None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
    except OSError as exc:
        logger.warning("hclfmt: failed to spawn %s (%s); returning unformatted", _BINARY, exc)

    return content
