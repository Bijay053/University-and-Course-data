"""Make malformed UTF-16 text safe for UTF-8 parsers without losing valid pairs."""
import re


def scalar_text(value: str) -> str:
    if not re.search(r"[\ud800-\udfff]", value):
        return value
    return value.encode("utf-16-le", errors="surrogatepass").decode("utf-16-le", errors="replace")
