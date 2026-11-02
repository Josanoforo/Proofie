"""Which text of a rendered page is Spanish but not inside an element that says so (WCAG 3.1.2, language of parts).

A word list, not a language detector: the Spanish words below are the ones the rule sets, the delivery hints and the
tracking-label glosses put into English pages. `spanish_outside_lang(html)` returns the Spanish words found in a text
node whose nearest `lang` ancestor is not Spanish."""
import re
from html.parser import HTMLParser

SPANISH_WORDS = re.compile(
    r"Protecci[oó]n|Vendedor|Comprobante de entrega|\bentregad[oa]\b|\bfirmad[oa]\b|\brecibid[oa]\b|\bocurre\b|"
    r"Última actualizaci|Qué reúne|requisitos|Paquetería|transportista", re.I)
_VOID = {"input", "br", "hr", "img", "meta", "link", "source", "area", "base", "col", "embed", "wbr"}


class _Scan(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.hits, self.skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            return
        self.stack.append((tag, dict(attrs).get("lang")))
        if tag in ("script", "style"):
            self.skip += 1

    def handle_endtag(self, tag):
        while self.stack:
            top, _ = self.stack.pop()
            if top in ("script", "style"):
                self.skip -= 1
            if top == tag:
                break

    def handle_data(self, data):
        if self.skip or not data.strip():
            return
        lang = next((value for _, value in reversed(self.stack) if value), "en")
        if lang.lower().startswith("es"):
            return
        self.hits.extend(m.group(0) for m in SPANISH_WORDS.finditer(data))


def spanish_outside_lang(html: str) -> list:
    scan = _Scan()
    scan.feed(html)
    return scan.hits
