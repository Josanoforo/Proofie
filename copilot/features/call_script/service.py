"""Pure bilingual call-script templates; no model, network, contact or messaging."""
TEMPLATES = {
    "en": {
        "opening": "Hello, I am calling about your recent purchase. I would like to understand what happened from your point of view.",
        "ask": "Could you check the mailbox, reception area, and anyone who may have accepted the parcel, such as a neighbor or building staff? Please tell me what you have already checked.",
        "avoid": "Do not promise a refund, delivery result, or dispute outcome. Do not ask the buyer to share passwords, payment details, or other sensitive information.",
        "close": "If you find the parcel or have another detail, please let me know. I will record what you tell me and review the case.",
    },
    "es": {
        "opening": "Hola, le llamo por una compra reciente. Quiero entender qué ocurrió desde su punto de vista.",
        "ask": "¿Podría revisar el buzón, la recepción y con cualquier persona que pudiera haber recibido el paquete, como un vecino o el personal del edificio? Cuénteme qué ya revisó.",
        "avoid": "No prometa un reembolso, una entrega ni un resultado de la disputa. No pida contraseñas, datos de pago ni otra información sensible.",
        "close": "Si encuentra el paquete o tiene otro detalle, por favor dígamelo. Registraré lo que me cuente y revisaré el caso.",
    },
}


def build(locale: str, status: str | None, stage: str | None) -> dict:
    if locale not in TEMPLATES:
        raise ValueError("Choose English or Spanish for the buyer.")
    return {"locale": locale, "sections": dict(TEMPLATES[locale]),
            "paypal_status": status if isinstance(status, str) else "Not returned",
            "paypal_stage": stage if isinstance(stage, str) else "Not returned",
            "source_label": "Case status and stage from a fresh PayPal dispute read; script text from a fixed template."}
