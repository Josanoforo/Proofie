from copilot.core.content_type import detect_content_type

MAX_TEXT_CHARS = 1600
MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_TYPES = {"image/jpeg", "image/png"}
SOURCE_PARTIES = {"buyer", "packer", "reception", "neighbor", "carrier"}


def validate_source(value: str) -> str:
    source = str(value or "").strip().casefold()
    if source not in SOURCE_PARTIES:
        raise ValueError("Choose who provided this third-party statement.")
    return source


def validate_text(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT_CHARS:
        raise ValueError("Enter a third-party statement under 1,600 characters.")
    return value.strip()


def validate_image(data: bytes) -> str:
    if not isinstance(data, bytes) or not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Choose a JPEG or PNG photo under 5 MB.")
    content_type = detect_content_type(data)
    if content_type not in IMAGE_TYPES:
        raise ValueError("The file contents are not a supported JPEG or PNG photo.")
    return content_type


def uncorroborated_label(source: str, recorded_at: str) -> str:
    return f"Third-party statement from {validate_source(source)}, uncorroborated · recorded locally at {recorded_at} (UTC)"
