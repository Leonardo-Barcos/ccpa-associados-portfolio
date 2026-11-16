"""Jinja2 environment for the membership pages."""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi.templating import Jinja2Templates

from . import brand
from .config import settings
from .paths import TEMPLATES_DIR

log = logging.getLogger("ccpa.templates")


def _para_timezone():
    """Timezone of Pará state.

    Windows ships no IANA timezone database (and slim Docker images do not
    either), which is why `tzdata` is a dependency. If it is still missing we
    fall back to a fixed UTC-3 rather than refusing to start: Brazil dropped
    daylight saving time in 2019, so the result is identical.
    """
    try:
        return ZoneInfo("America/Belem")
    except (ZoneInfoNotFoundError, KeyError):
        log.warning("tzdata missing — falling back to a fixed UTC-3 offset.")
        return timezone(timedelta(hours=-3), "America/Belem")


TZ = _para_timezone()

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

templates.env.globals["brand"] = brand
templates.env.globals["app_name"] = settings.app_name
templates.env.globals["base_url"] = settings.base_url
templates.env.globals["year"] = datetime.now(TZ).year
