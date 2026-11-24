"""Prompt builder for avatar generation (Qwen-Image-Edit). Every prompt edits the person's ORIGINAL photo,
so identity is preserved no matter what style, outfit or accessories they pick."""
import re

STYLES = {
    "3d": ("3D animated", "a polished 3D animated-film character portrait with soft stylized features and smooth shading"),
    "illustrated": ("Illustrated", "a clean modern digital illustration portrait with gentle painterly brushwork and warm colours"),
    "anime": ("Anime", "a high-quality anime-style character portrait with clean line art and soft cel shading"),
    "studio": ("Studio photo", "a professional studio headshot photograph with flattering soft lighting and sharp focus"),
    "watercolor": ("Watercolor", "a soft watercolor painting portrait with delicate washes and paper texture"),
    "comic": ("Comic", "a bold comic-book style portrait with confident ink lines and flat vivid colours"),
}
OUTFITS = {
    "keep": ("Keep my outfit", ""),
    "tee": ("Casual tee", "wearing a simple casual crew-neck t-shirt"),
    "hoodie": ("Hoodie", "wearing a comfortable hoodie"),
    "suit": ("Business suit", "wearing a sharp tailored business suit with a shirt and tie"),
    "blazer": ("Blazer & shirt", "wearing a smart blazer over an open-collar shirt"),
    "kurta": ("Kurta", "wearing an elegant kurta"),
    "saree": ("Saree", "wearing an elegant saree"),
    "traditional": ("Traditional formal", "wearing elegant traditional formal attire from their culture"),
    "sporty": ("Sporty", "wearing a sporty athletic jacket"),
    "doctor": ("Doctor's coat", "wearing a white doctor's coat over a shirt"),
    "chef": ("Chef's jacket", "wearing a white chef's jacket"),
    "leather": ("Leather jacket", "wearing a stylish leather jacket"),
    "sweater": ("Cozy sweater", "wearing a cozy knit sweater"),
}
ACCESSORIES = {
    "glasses": ("Glasses", "wearing stylish clear eyeglasses"),
    "headphones": ("Headphones", "with over-ear headphones resting around the neck"),
    "cap": ("Cap", "wearing a baseball cap"),
    "beanie": ("Beanie", "wearing a knit beanie"),
    "earrings": ("Earrings", "wearing small earrings"),
    "necklace": ("Necklace", "wearing a simple necklace"),
    "scarf": ("Scarf", "wearing a soft scarf"),
    "bindi": ("Bindi", "wearing a small bindi"),
    "turban": ("Turban", "wearing a neatly tied turban"),
    "hijab": ("Hijab", "wearing an elegant hijab"),
    "flower": ("Flower in hair", "with a flower in the hair"),
}
BACKGROUNDS = {
    "plain": ("Plain", "a plain softly blurred neutral background"),
    "gradient": ("Gradient", "a smooth colourful gradient background"),
    "office": ("Office", "a softly blurred modern office background"),
    "city": ("City", "a softly blurred city skyline background"),
    "beach": ("Beach", "a softly blurred sunny beach background"),
    "nature": ("Nature", "a softly blurred green nature background"),
    "room": ("Cozy room", "a softly blurred cozy living room background"),
    "studio": ("Studio lights", "a dark studio background with soft rim lighting"),
}

IDENTITY = ("Keep the person's identity exactly: same face shape, eyes, nose, lips, skin tone, hair colour, "
            "facial hair and age. Head-and-shoulders portrait, facing the camera directly, eyes open and visible, "
            "neutral friendly expression with the mouth gently closed, centered. Tasteful, fully clothed, "
            "safe for work. No text, no watermark, no hands, no extra people.")

_BLOCK = re.compile(r"\b(nude|naked|nsfw|topless|lingerie|sexy|sexual|explicit|gore|blood|nazi|swastika|"
                    r"kkk|weapon|gun|knife|cigarette|drugs?)\b", re.I)


def clean_extra(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()[:160]
    if _BLOCK.search(text):
        raise ValueError("Please keep avatar details tasteful (no explicit, violent or hateful content).")
    return text


def build_prompt(style: str, outfit: str = "keep", accessories: list[str] | None = None,
                 background: str = "plain", extra: str = "") -> tuple[str, str]:
    """Returns (prompt, human label)."""
    if style not in STYLES:
        raise ValueError("Unknown style")
    items, label = [], [STYLES[style][0]]
    if outfit in OUTFITS and OUTFITS[outfit][1]:
        items.append(OUTFITS[outfit][1])
        label.append(OUTFITS[outfit][0])
    accs = [a for a in (accessories or []) if a in ACCESSORIES][:4]
    items += [ACCESSORIES[a][1] for a in accs]
    label += [ACCESSORIES[a][0] for a in accs]
    bg = background if background in BACKGROUNDS else "plain"
    prompt = f"Transform this photo into {STYLES[style][1]}"
    if items:
        prompt += " of the person " + ", ".join(items)
    prompt += f", with {BACKGROUNDS[bg][1]}."
    extra = clean_extra(extra)
    if extra:
        prompt += f" Additional details: {extra}."
    return prompt + " " + IDENTITY, " · ".join(label)


def preset_prompts() -> dict:
    """The four starter avatars made right after upload."""
    return {s: build_prompt(s, background="studio" if s == "studio" else "plain")[0]
            for s in ("3d", "illustrated", "anime", "studio")}


def catalog() -> dict:
    pick = lambda d: [{"id": k, "label": v[0]} for k, v in d.items()]
    return {"styles": pick(STYLES), "outfits": pick(OUTFITS), "accessories": pick(ACCESSORIES),
            "backgrounds": pick(BACKGROUNDS)}
