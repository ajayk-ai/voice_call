"""Log masking."""


def mask_number(number: str) -> str:
    """Keep full phone numbers out of the logs."""
    return number[:4] + "*" * max(len(number) - 7, 0) + number[-3:]
