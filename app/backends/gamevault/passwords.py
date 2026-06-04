# app/backends/gamevault/passwords.py
import secrets

# Curated, inoffensive nouns (capitalized). Kept short so word+4 digits stays well within 6-32.
_WORDS: tuple[str, ...] = (
    "Tiger", "Eagle", "Falcon", "River", "Mountain", "Comet", "Galaxy", "Harbor",
    "Maple", "Cedar", "Willow", "Garnet", "Copper", "Silver", "Marble", "Canyon",
    "Meadow", "Summit", "Lantern", "Compass", "Anchor", "Beacon", "Cobalt", "Crystal",
    "Dolphin", "Ember", "Glacier", "Horizon", "Jasmine", "Juniper", "Kestrel", "Lotus",
    "Mango", "Nebula", "Olive", "Panther", "Quartz", "Raven", "Saffron", "Topaz",
    "Violet", "Walnut", "Yarrow", "Zephyr", "Almond", "Birch", "Cactus", "Dune",
    "Fjord", "Grove", "Hazel", "Indigo", "Lemon", "Onyx", "Pebble", "Reef",
    "Sage", "Thistle", "Umber", "Vetch", "Wren", "Acorn", "Bramble", "Coral",
)


def generate_memorable_password() -> str:
    """A memorable password: capitalized word + 4-digit number (e.g. 'Tiger4827').

    Satisfies GameVault's 6-32 character rule and is alphanumeric only.
    """
    word = secrets.choice(_WORDS)
    number = secrets.randbelow(9000) + 1000  # 1000..9999
    return f"{word}{number}"
