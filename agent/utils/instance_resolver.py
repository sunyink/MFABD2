"""Read MFAA v2.11.5+ instance identity; this is not the user's save number."""
import os

from . import mfaalog as logger


def resolve_instance_id() -> str | None:
    identity = os.environ.get("MFA_INSTANCE_ID", "").strip()
    if not identity:
        logger.info("[Resolver] 宿主未提供 MFA_INSTANCE_ID")
        return None
    name = os.environ.get("MFA_INSTANCE_NAME", "")
    logger.info(f"[Resolver] instance_id={identity}" + (f" ({name})" if name else ""))
    return identity
