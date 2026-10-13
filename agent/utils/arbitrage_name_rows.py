"""Separate low-confidence price-line glyphs from real product-name anchors."""
import re


def is_price_subrow_noise(token, name_tokens, anchors):
    """Require a confirmed preceding name and an adjacent base-price number.

    This is not an unknown-item whitelist. Chinese/truncated names, multi-
    character words, confident letters and first-row unknowns remain unknown.
    Coordinates are in MFABD2's native 1280-wide recognition space.
    """
    text = token.get('text', '').strip()
    if (len(text) != 1 or not text.isalpha() or '\u4e00' <= text <= '\u9fff'
            or token.get('score', 1) >= .6 or not anchors):
        return False
    previous = anchors[-1]
    if (not previous.get('identity', {}).get('confirmed')
            or not 24 <= token['cy'] - previous['cy'] <= 50):
        return False
    return any(
        re.fullmatch(r'[0-9]+', other.get('text', '').strip())
        and other.get('score', 0) >= .9
        and abs(other['cy']-token['cy']) <= 6
        and token['x']-5 <= other['x'] <= token['x']+token['w']+20
        for other in name_tokens
    )
