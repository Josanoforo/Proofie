"""Register the pure send-text helper only when the feature flag is enabled."""
from fastapi import APIRouter

from copilot.features.read_aloud_review.service import outgoing_notes
from copilot.web.routes_disputes import templates
from copilot.web.routes_package import templates as package_templates

router = APIRouter()
templates.env.globals["read_aloud_review_text"] = outgoing_notes
package_templates.env.globals["read_aloud_review_text"] = outgoing_notes      # the package page renders the entry
