"""Add public Halaqah posts to the Pages snapshot, keeping archived IDs and media."""
import argparse
import csv
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlencode, urljoin, urlsplit

from lxml import html

API = "https://ilmiyyah.com/wp-json/wp/v2/"
CATEGORY = 28
DATA = re.compile(r'(<script id="data" type="application/json">)(.*?)(</script>)', re.S)
VERSION = re.compile(r'(<meta name="halaqah-version" content=")[^"]*(">)')
ALLOWED = set("div p br hr span strong b i em u s small sub sup blockquote ol ul li h1 h2 h3 h4 h5 h6 figure figcaption img a table thead tbody tfoot td th tr pre code dl dt dd".split())
IMAGE_HOSTS = {"ilmiyyah.com", "www.ilmiyyah.com", "lh3.googleusercontent.com", "i0.wp.com", "i1.wp.com", "i2.wp.com"}


def public_json(path, **params):
    with tempfile.TemporaryDirectory() as directory:
        headers = Path(directory) / "headers"
        url = API + path + "?" + urlencode(params)
        result = subprocess.check_output([
            "curl", "-fsSL", "--proto", "=https", "--proto-redir", "=https",
            "--retry", "2", "--max-time", "35", "-D", str(headers), url
        ])
        total = re.findall(r"(?im)^x-wp-total:\s*(\d+)", headers.read_text())
        data = json.loads(result)
        if not isinstance(data, list) or not total:
            raise ValueError("Unexpected WordPress response")
        return data, int(total[-1])


def require_complete(rows, total, field="id"):
    if len(rows) != total or len({row[field] for row in rows}) != total:
        raise ValueError("Source response was incomplete or changed during sync")


def public_url(base, value):
    if not value:
        return ""
    address = urljoin(base, value.strip())
    parsed = urlsplit(address)
    if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
        return address
    if parsed.scheme == "http" and parsed.hostname == "ilmiyyah.com" and not parsed.username and not parsed.password:
        return parsed._replace(scheme="https").geturl()
    return ""


def image_location(address, root):
    if urlsplit(address).hostname not in IMAGE_HOSTS:
        return address
    stem = "s-" + hashlib.sha256(address.encode()).hexdigest()[:24]
    assets = root / "images"
    for existing in assets.glob(stem + ".*"):
        return "images/" + existing.name
    result = subprocess.run([
        "curl", "-fsSL", "--proto", "=https", "--proto-redir", "=https", "--max-time", "12",
        "--max-filesize", "6500000", address
    ], capture_output=True)
    if result.returncode:
        return address
    data = result.stdout
    formats = [(b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"), (b"GIF8", ".gif")]
    suffix = next((ext for magic, ext in formats if data.startswith(magic)), "")
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        suffix = ".webp"
    if not suffix:
        return address
    assets.mkdir(exist_ok=True)
    (assets / (stem + suffix)).write_bytes(data)
    return "images/" + stem + suffix


def convert(row, category_names, ids, root):
    url = public_url(API, row["link"])
    if urlsplit(url).hostname != "ilmiyyah.com":
        raise ValueError("Unrecognized article URL")
    source = row["content"]["rendered"]
    title = html.fragment_fromstring(row["title"]["rendered"], create_parent="div").text_content().strip()
    doc = html.fragment_fromstring(source, create_parent="div")
    if not title or not doc.text_content().strip():
        raise ValueError(f"Missing title or transcript for {row['id']}")
    audio = []
    for file in dict.fromkeys(doc.xpath(".//audio/@src | .//audio/source/@src")):
        stream = public_url(url, file)
        if stream:
            path = "audio/" + hashlib.sha256(file.encode()).hexdigest()[:24] + ".mp3"
            audio.append({"url": url, "file": path, "stream": stream, "downloaded": (root / path).is_file()})
    for node in doc.xpath('.//figure[contains(@class,"wp-block-audio")] | .//audio | .//script | .//style | .//iframe | .//form | .//svg | .//object | .//div[contains(@class,"pdfprnt-buttons")]'):
        if doc in node.iterancestors():
            node.drop_tree()
    for node in list(doc.iterdescendants()):
        if not isinstance(node.tag, str):
            node.getparent().remove(node)
            continue
        if node.tag not in ALLOWED:
            node.drop_tag()
            continue
        attributes = dict(node.attrib)
        node.attrib.clear()
        if attributes.get("dir") in ("ltr", "rtl", "auto"):
            node.set("dir", attributes["dir"])
        if re.search(r"text-align:\s*right", attributes.get("style", "")):
            node.set("style", "text-align:right")
        if node.tag == "a":
            link = public_url(url, attributes.get("href"))
            local = re.fullmatch(r"/archives/(\d+)/?", urlsplit(link).path)
            if local and urlsplit(link).hostname == "ilmiyyah.com" and int(local[1]) in ids:
                node.set("href", "#materi-" + local[1])
            elif link:
                node.attrib.update({"href": link, "target": "_blank", "rel": "noopener noreferrer"})
        if node.tag == "img":
            address = public_url(url, attributes.get("src") or attributes.get("data-src"))
            if address:
                node.attrib.update({
                    "src": image_location(address, root), "alt": attributes.get("alt", ""),
                    "loading": "lazy", "referrerpolicy": "no-referrer"
                })
            else:
                node.drop_tree()
    text = " ".join(" ".join(doc.xpath(".//text()")).split())
    if not text:
        raise ValueError(f"Transcript for {row['id']} is empty after cleanup")
    series = next((category_names[c] for c in row["categories"] if c in category_names), "Materi umum")
    return dict(id=row["id"], title=title, url=url, series=series, body=html.tostring(doc, encoding="unicode"), text=text, audio=audio)


def sync(root):
    index = root / "index.html"
    page = index.read_text(encoding="utf-8")
    embedded = DATA.search(page)
    if not embedded or not VERSION.search(page):
        raise ValueError("Invalid site snapshot")
    old = json.loads(embedded[2])
    by_id = {row["id"]: row for row in old}
    if not old or len(by_id) != len(old):
        raise ValueError("Duplicate or missing saved articles")

    children, total = public_json("categories", parent=CATEGORY, per_page=100, hide_empty="false", _fields="id,name")
    require_complete(children, total)
    names = {row["id"]: html.fragment_fromstring(row["name"], create_parent="div").text_content() for row in children}
    query = {"categories": ",".join(str(n) for n in [CATEGORY, *names]), "orderby": "date", "order": "desc"}
    first, total = public_json("posts", **query, per_page=100, page=1, _fields="id")
    if total == len(old) and [row["id"] for row in first] == [row["id"] for row in old[:100]]:
        print("Catalog is up to date.")
        return
    catalog = first[:]
    for number in range(2, (total + 99) // 100 + 1):
        more, reported = public_json("posts", **query, per_page=100, page=number, _fields="id")
        if reported != total:
            raise ValueError("Catalog total changed during sync")
        catalog.extend(more)
    require_complete(catalog, total)
    ids = [row["id"] for row in catalog]
    id_set = set(ids)
    if not set(by_id).issubset(id_set):
        raise ValueError("Source omitted previously archived posts. Keeping last complete release.")
    check_head, check_total = public_json("posts", **query, per_page=100, page=1, _fields="id")
    if check_total != total or check_head != first:
        raise ValueError("Catalog changed while checking it")
    new_ids = [i for i in ids if i not in by_id]
    for start in range(0, len(new_ids), 25):
        batch = new_ids[start:start + 25]
        posts, count = public_json("posts", include=",".join(map(str, batch)), per_page=25,
                                   _fields="id,title,content,link,categories")
        require_complete(posts, count)
        if {row["id"] for row in posts} != set(batch):
            raise ValueError("Missing transcript from WordPress")
        for row in posts:
            by_id[row["id"]] = convert(row, names, id_set, root)
    if not new_ids and ids == [row["id"] for row in old]:
        print("Catalog is up to date.")
        return
    updated = [dict(by_id[i], page=position // 3 + 1) for position, i in enumerate(ids)]
    encoded = encode_catalog(updated)
    page = page[:embedded.start(2)] + encoded + page[embedded.end(2):]
    write_release(root, page, updated)
    print(f"Added {len(new_ids)} new articles. Total: {len(updated)}.")


def encode_catalog(rows):
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e")


def write_release(root, page, rows):
    neutral = VERSION.sub(r"\g<1>\g<2>", page, count=1)
    revision = hashlib.sha256(neutral.encode()).hexdigest()[:20]
    page = VERSION.sub(lambda match: match[1] + revision + match[2], page, count=1)
    (root / "index.html").write_text(page, encoding="utf-8")
    with (root / "Katalog.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["ID", "Judul", "Seri", "Halaman arsip", "Audio offline", "Sumber"])
        for row in rows:
            writer.writerow([row["id"], row["title"], row["series"], row["page"],
                             sum(a.get("downloaded", False) for a in row["audio"]), row["url"]])
    version = {"revision": revision, "count": len(rows), "published": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    (root / "updates.json").write_text(json.dumps(version, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()
    sync(args.root)
