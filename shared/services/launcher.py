"""
shared.services.launcher — generic ``python -m`` entrypoint for
the Phase 22 systemd template ``tickles-service@.service``.

Given a service name registered in
:data:`shared.services.SERVICE_REGISTRY`, resolve its factory
(which must return an object with ``run_forever()``) and block on
it. If the service has no factory, delegate to the module listed
on the descriptor via ``python -m <module>`` semantics.

This keeps the systemd unit file identical across every service —
only the instance name (``%i``) changes.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import runpy
import sys

from shared.services import SERVICE_REGISTRY

logger = logging.getLogger("tickles.services.launcher")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="tickles-service-launcher")
    p.add_argument("--name", required=True, help="service name in SERVICE_REGISTRY")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        desc = SERVICE_REGISTRY.get(args.name)
    except KeyError:
        logger.error("service not registered: %s", args.name)
        return 2

    if desc.factory is not None:
        obj = desc.factory()
        run_forever = getattr(obj, "run_forever", None)
        if run_forever is None:
            logger.error("factory for %s returned object without run_forever()", args.name)
            return 2
        asyncio.run(run_forever())
        return 0

    logger.info(
        "no factory registered for %s; delegating to module %s",
        args.name,
        desc.module,
    )
    sys.argv = [desc.module]
    runpy.run_module(desc.module, run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
