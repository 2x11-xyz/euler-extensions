"""Shared model-facing text safety predicates for Euler extensions."""

from __future__ import annotations


_FORMAT_SPOOF_RANGES = (
    (0x00AD, 0x00AD),
    (0x0600, 0x0605),
    (0x061C, 0x061C),
    (0x06DD, 0x06DD),
    (0x070F, 0x070F),
    (0x0890, 0x0891),
    (0x08E2, 0x08E2),
    (0x180E, 0x180E),
    (0x200B, 0x200F),
    (0x2028, 0x2029),
    (0x202A, 0x202E),
    (0x2060, 0x2064),
    (0x2066, 0x206F),
    (0xFEFF, 0xFEFF),
    (0xFFF9, 0xFFFB),
    (0x110BD, 0x110BD),
    (0x110CD, 0x110CD),
    (0x13430, 0x1343F),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0001, 0xE0001),
    (0xE0020, 0xE007F),
)


def extension_model_text_is_format_safe(text: str) -> bool:
    """Reject Euler's frozen Unicode-17 format-spoof and separator set.

    This predicate deliberately does not check ordinary control characters,
    emptiness, or length. Those structural rules belong to the host operation
    or workflow that admits the text.
    """

    if not isinstance(text, str):
        raise TypeError("model text must be a string")
    return not any(_is_format_spoof(character) for character in text)


def _is_format_spoof(character: str) -> bool:
    code_point = ord(character)
    return any(start <= code_point <= end for start, end in _FORMAT_SPOOF_RANGES)
