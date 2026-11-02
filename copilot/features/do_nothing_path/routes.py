from fastapi import APIRouter

from copilot.features.do_nothing_path.service import describe
from copilot.web.routes_disputes import templates

router = APIRouter()
# The resolution page already has a fresh PayPal snapshot and the existing integer-cent economics result.
# Attach this deterministic helper only when the feature's lazy-import flag is on.
templates.env.globals["do_nothing_path"] = describe
