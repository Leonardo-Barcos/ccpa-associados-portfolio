"""Member companies' logos — the strip on the signup page.

Uploaded by an administrator in the panel, stored in the database (see
`BrandLogo`) and served by `/marcas/{id}`.

Whatever arrives is redrawn before it is stored:
  * a PDF (what designers usually send) is rendered page by page — a
    logo PDF often carries several versions, one per page, and the panel
    lets the admin pick one;
  * PNG, JPG and WEBP are decoded;
  * then the empty margins are cut off, the image is scaled down to what the
    strip needs and saved as a fresh PNG.

Re-encoding means nothing from the original file reaches a visitor but the
pixels. SVG is refused: it can carry JavaScript, and a logo is served from
our own domain, so a hostile SVG would run as the site.
"""

from __future__ import annotations

import base64
import binascii
import io
import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import BrandLogo

log = logging.getLogger("ccpa.marcas")

# Vercel refuses request bodies over 4.5 MB; stay under it with room for the
# rest of the form.
MAX_BYTES = 4 * 1024 * 1024
# Stored size: plenty for a 72 px tall chip on a 3x phone screen.
MAX_WIDTH, MAX_HEIGHT = 900, 360
MAX_PDF_PAGES = 8
# A 10 000 x 10 000 image is a "decompression bomb", not a logo.
MAX_PIXELS = 40_000_000
# Below this many logos the strip stands still, centred. A marquee with one
# or two logos just shows the same image chasing itself across the screen;
# from four on it has something to scroll through.
MARQUEE_MIN = 4


class LogoError(ValueError):
    """An upload that cannot be accepted; the message is for the panel."""


def sniff(data: bytes) -> str | None:
    """The file's real type, from its signature — or None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.lstrip()[:5] == b"%PDF-":
        return "application/pdf"
    return None


def clean_website(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    if not value.lower().startswith(("http://", "https://")):
        value = "https://" + value
    if " " in value or "." not in value.split("//", 1)[-1]:
        raise LogoError("Site inválido. Ex.: www.empresa.com.br")
    return value[:255]


# --------------------------------------------------------------------------
#  Image processing
# --------------------------------------------------------------------------
def _trim(image):
    """Cut the empty margin around the drawing.

    "Empty" is transparent or near-white: logos exported from Illustrator
    sit in the middle of a big artboard, and on the strip that margin would
    shrink the logo to a speck inside its card.
    """
    from PIL import ImageChops

    alpha = image.getchannel("A")
    visible = alpha.point(lambda a: 255 if a > 8 else 0)
    white = image.convert("RGB").point(lambda v: 255 if v > 245 else 0).convert("L")
    # Ink = visible and not white. White pixels count as ink only when the
    # whole logo is white (then trimming by alpha alone is the best we can do).
    ink = ImageChops.subtract(visible, white.point(lambda v: 255 if v == 255 else 0))
    box = ink.getbbox() or visible.getbbox()
    return image.crop(box) if box else image


def _finish(image) -> bytes:
    image = _trim(image.convert("RGBA"))
    if image.width < 8 or image.height < 8:
        raise LogoError("A imagem está vazia ou pequena demais.")
    image.thumbnail((MAX_WIDTH, MAX_HEIGHT))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _raster(data: bytes) -> list[bytes]:
    from PIL import Image, UnidentifiedImageError

    try:
        image = Image.open(io.BytesIO(data))
        if image.format not in ("PNG", "JPEG", "WEBP"):
            raise LogoError("Formato não aceito. Envie PDF, PNG, JPG ou WEBP.")
        if image.width * image.height > MAX_PIXELS:
            raise LogoError("Imagem grande demais em pixels. Exporte com até 2000 px.")
        image.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise LogoError("Não foi possível ler a imagem. O arquivo está inteiro?") from exc
    return [_finish(image)]


def _pdf(data: bytes) -> list[bytes]:
    import pypdfium2 as pdfium

    try:
        document = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        raise LogoError(
            "Não foi possível abrir o PDF (protegido por senha ou corrompido)."
        ) from exc
    pages: list[bytes] = []
    try:
        for index in range(min(len(document), MAX_PDF_PAGES)):
            page = document[index]
            width, height = page.get_size()
            # About 1600 px on the long side before trimming: after the
            # artboard margin goes, what is left is still sharp.
            scale = min(1600 / max(width, height, 1), 8)
            bitmap = page.render(scale=scale, fill_color=(255, 255, 255, 0))
            try:
                pages.append(_finish(bitmap.to_pil()))
            except LogoError:
                continue  # a blank page, skipped
    finally:
        document.close()
    if not pages:
        raise LogoError("O PDF não tem nenhuma página com desenho.")
    # Two pages that render to the same pixels are one choice.
    unique: list[bytes] = []
    for png in pages:
        if png not in unique:
            unique.append(png)
    return unique


def whiten(png: bytes) -> bytes:
    """The logo in white, for the dark background of the hero.

    Dark ink becomes solid white and white fill becomes transparent — the
    way a designer makes the "negative" version. A black square with a white
    diamond inside stays a square with a diamond, instead of turning into a
    white block. Mid tones (a coloured logo) become partly transparent
    white, scaled so the darkest part of the logo is fully opaque.

    A logo that is already light (a white version was uploaded) is kept as
    a silhouette instead, or it would vanish.
    """
    from PIL import Image, ImageChops

    image = Image.open(io.BytesIO(png)).convert("RGBA")
    alpha = image.getchannel("A")
    lightness = image.convert("L")
    visible = alpha.point(lambda a: 255 if a > 8 else 0)

    histogram = lightness.histogram(mask=visible)
    total = sum(histogram) or 1
    mean = sum(value * count for value, count in enumerate(histogram)) / total
    if mean > 200:
        shape = alpha  # already light: keep its outline, paint it white
    else:
        darkest = next((v for v, count in enumerate(histogram) if count), 0)
        span = max(255 - darkest, 1)
        # The 1.15 lets near-black print as full white: a logo's "black" is
        # often a dark grey, and would otherwise come out as a dull white.
        ink = lightness.point(
            lambda v: max(0, min(255, int((255 - v) * 255 / span * 1.15)))
        )
        shape = ImageChops.multiply(ink, alpha)

    white = Image.new("RGBA", image.size, (255, 255, 255, 0))
    white.putalpha(shape)
    buffer = io.BytesIO()
    white.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def rendered(logo) -> bytes:
    """The image as the page shows it, according to the logo's tone."""
    if logo.tone == "original":
        return logo.image
    return whiten(logo.image)


def prepare(data: bytes) -> list[bytes]:
    """The upload as ready-to-store PNGs: one per version found.

    A PDF with several pages yields several options; anything else yields
    exactly one.
    """
    if not data:
        raise LogoError("Escolha o arquivo da logo.")
    if len(data) > MAX_BYTES:
        raise LogoError(
            f"Arquivo grande demais (máximo {MAX_BYTES // (1024 * 1024)} MB)."
        )
    kind = sniff(data)
    if kind == "application/pdf":
        return _pdf(data)
    if kind is None:
        raise LogoError(
            "Formato não aceito. Envie PDF, PNG, JPG ou WEBP "
            "(SVG não é aceito por segurança)."
        )
    return _raster(data)


def as_data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def from_choice(value: str) -> bytes:
    """Read back the option the admin picked on the "choose a version" page.

    It comes from the browser, so it is processed again like any upload:
    only a real PNG gets through, and it is redrawn before it is stored.
    """
    try:
        data = base64.b64decode(value.split(",", 1)[-1], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise LogoError("Escolha uma das versões.") from exc
    if sniff(data) != "image/png":
        raise LogoError("Escolha uma das versões.")
    return prepare(data)[0]


# --------------------------------------------------------------------------
#  Storage
# --------------------------------------------------------------------------
def ordered(db: Session, *, only_active: bool = False) -> list[BrandLogo]:
    query = select(BrandLogo)
    if only_active:
        query = query.where(BrandLogo.active.is_(True))
    return list(db.scalars(query.order_by(BrandLogo.position, BrandLogo.id)))


def public_logos(db: Session) -> list[BrandLogo]:
    """Logos for the signup page. Never breaks the page: without the strip
    the form still works, and the form is what matters."""
    try:
        return ordered(db, only_active=True)
    except Exception:  # noqa: BLE001
        log.exception("Could not load brand logos")
        db.rollback()
        return []


def add(
    db: Session,
    name: str,
    website: str | None,
    png: bytes,
    member_id: str | None = None,
) -> BrandLogo:
    """Store a logo already produced by `prepare`."""
    if len(name.strip()) < 2:
        raise LogoError("Informe o nome da empresa.")
    last = db.scalar(select(func.max(BrandLogo.position))) or 0
    logo = BrandLogo(
        name=name.strip()[:120],
        website=clean_website(website),
        image=png,
        content_type="image/png",
        position=last + 1,
        active=True,
        member_id=member_id,
    )
    db.add(logo)
    db.commit()
    return logo


def move(db: Session, logo: BrandLogo, direction: int) -> bool:
    """Swap the logo with its neighbour; False when it is already at the end."""
    logos = ordered(db)
    index = next(i for i, item in enumerate(logos) if item.id == logo.id)
    other = index + direction
    if not 0 <= other < len(logos):
        return False
    # Renumber everything: positions may have gaps or ties after removals.
    logos[index], logos[other] = logos[other], logos[index]
    for position, item in enumerate(logos, start=1):
        item.position = position
    db.commit()
    return True


def marquee_copies(count: int) -> int:
    """How many times the list is repeated inside one half of the track.

    The animation slides the track by exactly half its width, so each half
    has to be at least as wide as the screen or a gap opens on the right. A
    short list is repeated until it has enough logos to cover it.
    """
    if count <= 0:
        return 0
    minimum = 8
    return max(1, -(-minimum // count))
