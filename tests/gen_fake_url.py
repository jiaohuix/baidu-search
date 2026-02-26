import hashlib
from urllib.parse import urlparse


def generate_cite_url(real_url: str) -> str:
    """
    Generate a short virtual citation URL.
    Format:
        cite://{domain}/{sha1(real_url)[:10]}
    Args:
        real_url: The original full URL.
    Returns:
        Short virtual citation URL.
    """
    parsed = urlparse(real_url)
    domain = parsed.netloc.lower()
    # remove www.
    if domain.startswith("www."):
        domain = domain[4:]

    hash_id = hashlib.sha1(real_url.encode("utf-8")).hexdigest()[:10]
    return f"cite://{domain}/{hash_id}"


if __name__ == "__main__":
    url = '''https://www.baidu.com/baidu.php?url=K60000avpXkFvm72079fgQ5NdxcHruuRBkE5Ur-3r5Ng6'''
    short = generate_cite_url(url)
    print(short) # cite://baidu.com/e60bdd6912