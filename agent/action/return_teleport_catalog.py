"""Pure card catalog for teleport return; never reads gameplay save records."""

from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re


CATEGORIES = ("story", "character", "event")
_CATEGORY_INDEX = {name: index for index, name in enumerate(CATEGORIES)}
_MAIN_NODE = re.compile(r"Collect_Pack_(Story|Character|Event)_([1-9][0-9]*)\Z")
_SUPPLEMENT_NODE = re.compile(r"ReturnCards_Catalog_(Story|Character|Event)_([1-9][0-9]*)\Z")


class CatalogError(ValueError):
    """Invalid or incomplete resource data, distinct from a locked game card."""


@dataclass(frozen=True)
class ReturnCard:
    category: str
    number: int
    templates: tuple[str, ...]
    node_name: str


def _node_identity(name):
    match = _MAIN_NODE.fullmatch(name) or _SUPPLEMENT_NODE.fullmatch(name)
    if match is None or int(match[2]) > 100:
        return None
    return match[1].lower(), int(match[2])


def _template_name(value, *, node_name):
    if not isinstance(value, str) or not value or value.strip() != value:
        raise CatalogError(f"{node_name}: invalid template name {value!r}")
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if ("\x00" in value or ":" in value or PureWindowsPath(value).drive
            or PurePosixPath(normalized).is_absolute()
            or any(part in ("", ".", "..") for part in parts)):
        raise CatalogError(f"{node_name}: unsafe template path {value!r}")
    return normalized


def _image_exists(root, name, *, node_name, required):
    # Image resolution mirrors the PC-over-base resource precedence.
    for layer in ("pc", "base"):
        image_root = (root / layer / "image").resolve()
        path = image_root / name
        if not path.exists():
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(image_root) or not resolved.is_file():
            raise CatalogError(f"{node_name}: unsafe or non-file template {name!r}")
        return True
    if required:
        raise CatalogError(f"{node_name}: missing primary template {name!r}; not an unlock result")
    return False


def load_catalog(resource_root=None):
    """Read public and private PC card definitions, including disabled cards.

    Missing primary images and invalid paths raise CatalogError. Optional local
    active-cover images are added only when present. The returned catalog is a
    list of candidates, not evidence that any card is unlocked for an account.
    ReturnCards_Catalog_* supplements extend a card's fallback images; an
    official Collect_Pack_* definition always remains first and owns node_name.
    """
    if resource_root is not None:
        root = Path(resource_root).resolve()
    else:
        base_dir = Path(__file__).resolve().parents[2]
        candidate = base_dir / "resource"
        root = (candidate if candidate.is_dir() else base_dir / "assets" / "resource").resolve()
    if not root.is_dir() or not (root / "base" / "pipeline").is_dir():
        raise CatalogError(f"Resource directory is unavailable: {root}")
    merged = {}
    origins = {}
    for layer in ("base", "pc"):
        directory = root / layer / "pipeline"
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*.json"), key=lambda item: item.relative_to(directory).as_posix()):
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, ValueError) as exc:
                raise CatalogError(f"Cannot read pipeline {path}: {exc}") from exc
            if not isinstance(data, dict):
                raise CatalogError(f"Pipeline must contain an object: {path}")
            for name, fields in data.items():
                if _node_identity(name) is None:
                    continue
                if not isinstance(fields, dict):
                    raise CatalogError(f"{path}: {name} must contain an object")
                merged.setdefault(name, {}).update(fields)
                origins[name] = path
    cards = {}
    official_names = {_node_identity(name): name for name in merged if name.startswith("Collect_Pack_")}
    # Apply resource-layer overrides within each node first, then join logical
    # card identities. New official nodes must not conflict with old supplements.
    names = sorted(merged, key=lambda name: (not name.startswith("Collect_Pack_"), name))
    for name in names:
        fields = merged[name]
        raw = fields.get("template")
        if raw is None or raw == []:
            continue
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            raise CatalogError(f"{origins[name]}: {name} template must be a string or list")
        templates = []
        for item in raw:
            relative = _template_name(item, node_name=name)
            _image_exists(root, relative, node_name=name, required=True)
            if relative not in templates:
                templates.append(relative)
        if not templates:
            continue
        category, number = _node_identity(name)
        key = (category, number)
        previous = cards.get(key)
        if previous is not None:
            templates = list(dict.fromkeys((*previous.templates, *templates)))
            name = previous.node_name
        else:
            name = official_names.get(key, name)
        cards[key] = ReturnCard(category, number, tuple(templates), name)
    result = []
    for card in cards.values():
        category, number = card.category, card.number
        templates = list(card.templates)
        if category == "story" and 1 <= number <= 8:
            extras = [f"Cartridges/Steal_sp{number}_active_PC.png"]
            if number == 4:
                extras.append("Cartridges/Steal_sp4_active_art_PC.png")
            for relative in extras:
                if (relative not in templates
                        and _image_exists(root, relative, node_name=card.node_name, required=False)):
                    templates.append(relative)
        result.append(ReturnCard(category, number, tuple(templates), card.node_name))
    return tuple(sorted(result, key=lambda card: (_CATEGORY_INDEX[card.category], card.number)))


def select_cards(catalog, scope):
    """Select current, story 1–8, or story 1–19/character 1–7/all events."""
    if scope not in ("current", "steal", "all"):
        raise CatalogError(f"Unknown teleport return scope: {scope!r}")
    if scope == "current":
        return ()
    unique = {}
    for card in catalog:
        if (not isinstance(card, ReturnCard) or card.category not in _CATEGORY_INDEX
                or type(card.number) is not int or not 1 <= card.number <= 100
                or not card.templates):
            raise CatalogError(f"Invalid card in teleport catalog: {card!r}")
        key = (card.category, card.number)
        if key in unique and unique[key] != card:
            raise CatalogError(f"Ambiguous card definition: {key}")
        unique[key] = card
    if scope == "steal":
        missing = [number for number in range(1, 9) if ("story", number) not in unique]
        if missing:
            raise CatalogError(f"Stealing scope requires story cards 1–8; missing: {missing}")
        return tuple(unique[("story", number)] for number in range(1, 9))
    allowed = (card for card in unique.values()
               if card.category == "event"
               or (card.category == "story" and card.number <= 19)
               or (card.category == "character" and card.number <= 7))
    return tuple(sorted(allowed, key=lambda card: (_CATEGORY_INDEX[card.category], card.number)))
