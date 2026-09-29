"""Cross-generation character popularity prior.

SOURCE: the official **2020 "Pokemon of the Year" global poll** (Pokemon Day
2020 — a worldwide fan vote whose results were announced Feb 27, 2020). The
published per-region Top-30 tables (each with global vote counts) merged and
re-sorted by vote count yield a verified overall ranking 240 deep; ranks 1-30
here exactly match Bulbapedia's published overall Top 30. Vote counts beyond
rank 240 were never released publicly.

  Bulbapedia: https://bulbapedia.bulbagarden.net/wiki/Pok%C3%A9mon_of_the_Year

WHY: this is a price-INDEPENDENT popularity ground truth covering Gen 1-8. It
anchors the character-premium feature so *genuine fame* drives the score instead
of in-set chase frequency (which only sees our handful of Scarlet & Violet sets
and inflates whichever modern Legendaries happen to have pricey chase cards).

HOW the 0-10 score is built — TWO recognition signals, combined by max():
  A favorites poll measures *enthusiast* preference (it over-rates competitive/
  design darlings like Hydreigon and under-rates household names like Mewtwo).
  So we pair it with a mainstream-recognition signal and take the better of the
  two — a Pokemon is famous if it's broadly recognized OR a fan favorite:
    * WIKIPEDIA pageviews (data/wiki_pageviews.json) — en.wikipedia 12-month
      article views = how many people actually look it up. Captures mainstream
      icons (Pikachu, Charizard, Mewtwo, Gengar, Eevee). log10 -> [6.5, 9.9].
    * 2020 POLL vote count — fan favorites (Greninja, Umbreon, Sylveon, Rayquaza
      — many of whom have no Wikipedia article). log10 -> [POLL_FLOOR, 9.3].
  This is the fix for the "Hydreigon 9.6 / Mewtwo 7" problem: Mewtwo's 223k
  Wikipedia views lift it to ~9.3 despite a poll rank of 39, while Hydreigon (no
  article, poll rank 27) stays ~8.0.
  * USER_OVERRIDES win over both (hand-pinned anchors). 10.0 is reserved for them
    so the user's named icons (Charizard/Pikachu/Gengar) sit at the very top.
  * The scores are a STARTING POINT — pipeline/review_premium.py flags the
    biggest price-vs-recognition discrepancies for manual override.
  * Characters with NEITHER signal (Gen 9 newcomers, obscure mons) get no prior
    -> signals.py falls back to the structural signal, compressed BELOW
    POLL_FLOOR so any recognized Pokemon outranks any unrecognized one.

Mega / regional / form variants inherit the base Pokemon's score
("Mega Charizard Y" -> Charizard).
"""
from __future__ import annotations

import json
import math
import os
from typing import Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# Hand-pinned anchors — these WIN over the poll. 10.0 is reserved for them.
# (Set by the user: the marquee chase characters that command a card premium
# regardless of where a favorites poll ranks them — e.g. Pikachu is only poll
# rank 19 but is a perennial card-value driver.)
# ---------------------------------------------------------------------------
USER_OVERRIDES: Dict[str, float] = {
    "Charizard": 10.0,
    "Pikachu": 10.0,
    "Gengar": 10.0,
    # Hand-pinned by the user from the review pass — marquee icons the blended
    # signal under-rated (the poll takes them for granted; their durable card
    # value is higher). Add a line here to pin any character.
    "Mew": 9.5,
    "Mewtwo": 9.5,
    "Snorlax": 9.3,
    "Eevee": 9.5,
    "Rayquaza": 9.0,
    # NOTE: the Kanto starters are no longer hand-pinned here — the systematic
    # Kanto (Gen-1) nostalgia tilt in signals.char_premium_table lifts the whole
    # region by +0.75 (capped 9.5), preserving the data-driven intra-region
    # ranking. Charizard/Pikachu stay pinned at 10.0 as marquee top icons.
}

# ---------------------------------------------------------------------------
# 2020 Pokemon of the Year — name -> global vote count (overall rank in comment).
# ---------------------------------------------------------------------------
POLL_VOTES: Dict[str, int] = {
    "Greninja": 140559,    # 1
    "Lucario": 102259,     # 2
    "Mimikyu": 99077,      # 3
    "Charizard": 93968,    # 4
    "Umbreon": 67062,      # 5
    "Sylveon": 66029,      # 6
    "Garchomp": 61877,     # 7
    "Rayquaza": 60939,     # 8
    "Gardevoir": 60596,    # 9
    "Gengar": 60214,       # 10
    "Dragapult": 57973,    # 11
    "Tyranitar": 56834,    # 12
    "Bulbasaur": 56015,    # 13
    "Toxtricity": 55032,   # 14
    "Lugia": 53268,        # 15
    "Rowlet": 52367,       # 16
    "Aegislash": 51517,    # 17
    "Chandelure": 50943,   # 18
    "Pikachu": 48060,      # 19
    "Eevee": 47762,        # 20
    "Luxray": 46032,       # 21
    "Decidueye": 44011,    # 22
    "Zoroark": 43782,      # 23
    "Lycanroc": 42792,     # 24
    "Corviknight": 41711,  # 25
    "Flygon": 41420,       # 26
    "Hydreigon": 40054,    # 27
    "Sceptile": 38724,     # 28
    "Blaziken": 38307,     # 29
    "Snom": 38034,         # 30
    "Mudkip": 36920,       # 31
    "Dragonite": 36873,    # 32
    "Mew": 36266,          # 33
    "Metagross": 35631,    # 34
    "Typhlosion": 35184,   # 35
    "Noivern": 34795,      # 36
    "Scizor": 34691,       # 37
    "Piplup": 34680,       # 38
    "Mewtwo": 34585,       # 39
    "Infernape": 33267,    # 40
    "Ampharos": 32009,     # 41
    "Zeraora": 31691,      # 42
    "Alcremie": 30612,     # 43
    "Darkrai": 30544,      # 44
    "Goodra": 30209,       # 45
    "Espeon": 30052,       # 46
    "Incineroar": 29925,   # 47
    "Arcanine": 29795,     # 48
    "Jirachi": 29611,      # 49
    "Cyndaquil": 28332,    # 50
    "Milotic": 28295,      # 51
    "Absol": 27781,        # 52
    "Golisopod": 26975,    # 53
    "Cinderace": 26892,    # 54
    "Swampert": 26540,     # 55
    "Suicune": 26227,      # 56
    "Glaceon": 26161,      # 57
    "Zacian": 26158,       # 58
    "Primarina": 25953,    # 59
    "Yamper": 25695,       # 60
    "Salamence": 24920,    # 61
    "Arceus": 24502,       # 62
    "Aron": 24389,         # 63
    "Volcarona": 24389,    # 64
    "Lapras": 23411,       # 65
    "Haxorus": 22937,      # 66
    "Totodile": 22526,     # 67
    "Talonflame": 22328,   # 68
    "Serperior": 22269,    # 69
    "Oshawott": 21990,     # 70
    "Empoleon": 21773,     # 71
    "Dedenne": 21691,      # 72
    "Crobat": 21548,       # 73
    "Zekrom": 21477,       # 74
    "Furret": 21447,       # 75
    "Giratina": 21366,     # 76
    "Wooloo": 21266,       # 77
    "Victini": 20957,      # 78
    "Leafeon": 20859,      # 79
    "Yveltal": 20852,      # 80
    "Inteleon": 20697,     # 81
    "Torterra": 20632,     # 82
    "Celebi": 20492,       # 83
    "Goomy": 20299,        # 84
    "Sirfetch'd": 20217,   # 85
    "Reshiram": 20123,     # 86
    "Scorbunny": 20058,    # 87
    "Snorlax": 19768,      # 88
    "Krookodile": 19628,   # 89
    "Ninetales": 19044,    # 90
    "Tyrantrum": 18778,    # 91
    "Hatterene": 18581,    # 92
    "Chikorita": 18521,    # 93
    "Squirtle": 18476,     # 94
    "Ho-Oh": 18278,        # 95
    "Feraligatr": 18245,   # 96
    "Aggron": 18120,       # 97
    "Whimsicott": 17855,   # 98
    "Latias": 17478,       # 99
    "Shaymin": 17465,      # 100
    "Xerneas": 17415,      # 101
    "Bewear": 17181,       # 102
    "Sobble": 17155,       # 103
    "Snivy": 17020,        # 104
    "Altaria": 16814,      # 105
    "Blastoise": 16795,    # 106
    "Heracross": 16577,    # 107
    "Solgaleo": 16274,     # 108
    "Meltan": 16077,       # 109
    "Falinks": 16009,      # 110
    "Appletun": 15989,     # 111
    "Natu": 15699,         # 112
    "Eternatus": 15699,    # 113
    "Pichu": 15695,        # 114
    "Kyogre": 15585,       # 115
    "Mawile": 15523,       # 116
    "Zorua": 14910,        # 117
    "Vaporeon": 14887,     # 118
    "Buzzwole": 14747,     # 119
    "Houndoom": 14742,     # 120
    "Hawlucha": 14607,     # 121
    "Scolipede": 14536,    # 122
    "Muk": 14358,          # 123
    "Pyukumuku": 14358,    # 124
    "Dialga": 14292,       # 125
    "Togepi": 14288,       # 126
    "Gallade": 14144,      # 127
    "Reuniclus": 14129,    # 128
    "Staraptor": 14054,    # 129
    "Charmander": 14049,   # 130
    "Litten": 14005,       # 131
    "Morpeko": 13945,      # 132
    "Meloetta": 13915,     # 133
    "Silvally": 13897,     # 134
    "Ditto": 13843,        # 135
    "Meowstic": 13661,     # 136
    "Fennekin": 13508,     # 137
    "Grookey": 13478,      # 138
    "Togekiss": 13426,     # 139
    "Vikavolt": 13375,     # 140
    "Quagsire": 13308,     # 141
    "Grimmsnarl": 12923,   # 142
    "Salazzle": 12863,     # 143
    "Breloom": 12801,      # 144
    "Kommo-o": 12790,      # 145
    "Gliscor": 12676,      # 146
    "Zamazenta": 12641,    # 147
    "Torchic": 12568,      # 148
    "Latios": 12487,       # 149
    "Slowpoke": 12369,     # 150
    "Melmetal": 12356,     # 151
    "Marshadow": 12107,    # 152
    "Groudon": 11982,      # 153
    "Zygarde": 11943,      # 154
    "Centiskorch": 11619,  # 155
    "Nidoking": 11586,     # 156
    "Samurott": 11444,     # 157
    "Dracovish": 11436,    # 158
    "Lopunny": 11411,      # 159
    "Excadrill": 11376,    # 160
    "Porygon": 11311,      # 161
    "Lilligant": 11292,    # 162
    "Vulpix": 11224,       # 163
    "Psyduck": 11212,      # 164
    "Raichu": 11196,       # 165
    "Pinsir": 11162,       # 166
    "Litwick": 11140,      # 167
    "Jolteon": 11064,      # 168
    "Dragonair": 11051,    # 169
    "Frosmoth": 10988,     # 170
    "Rockruff": 10986,     # 171
    "Bisharp": 10975,      # 172
    "Bidoof": 10924,       # 173
    "Diancie": 10918,      # 174
    "Tsareena": 10900,     # 175
    "Delphox": 10889,      # 176
    "Turtwig": 10865,      # 177
    "Weavile": 10854,      # 178
    "Deoxys": 10842,       # 179
    "Braixen": 10807,      # 180
    "Grovyle": 10746,      # 181
    "Banette": 10579,      # 182
    "Venusaur": 10454,     # 183
    "Articuno": 10450,     # 184
    "Froslass": 10408,     # 185
    "Entei": 10404,        # 186
    "Espurr": 10402,       # 187
    "Eiscue": 10392,       # 188
    "Kyurem": 10338,       # 189
    "Hoopa": 10327,        # 190
    "Aurorus": 10321,      # 191
    "Pancham": 10187,      # 192
    "Quilava": 10032,      # 193
    "Magnemite": 9955,     # 194
    "Porygon2": 9902,      # 195
    "Cinccino": 9892,      # 196
    "Lunala": 9887,        # 197
    "Dragalge": 9882,      # 198
    "Emolga": 9798,        # 199
    "Pachirisu": 9694,     # 200
    "Rotom": 9417,         # 201
    "Treecko": 9339,       # 202
    "Wooper": 9323,        # 203
    "Pumpkaboo": 9162,     # 204
    "Roserade": 9092,      # 205
    "Trevenant": 9019,     # 206
    "Sableye": 9012,       # 207
    "Keldeo": 8915,        # 208
    "Lurantis": 8889,      # 209
    "Pangoro": 8859,       # 210
    "Mismagius": 8833,     # 211
    "Joltik": 8796,        # 212
    "Minccino": 8739,      # 213
    "Ludicolo": 8708,      # 214
    "Obstagoon": 8705,     # 215
    "Rillaboom": 8625,     # 216
    "Togedemaru": 8531,    # 217
    "Cramorant": 8436,     # 218
    "Shinx": 8373,         # 219
    "Phantump": 8231,      # 220
    "Toxapex": 8221,       # 221
    "Misdreavus": 8156,    # 222
    "Shuckle": 8129,       # 223
    "Tapu Koko": 8090,     # 224
    "Golurk": 8035,        # 225
    "Genesect": 7905,      # 226
    "Mudsdale": 7787,      # 227
    "Wobbuffet": 7745,     # 228
    "Necrozma": 7739,      # 229
    "Popplio": 7542,       # 230
    "Cosmog": 7515,        # 231
    "Kingdra": 7418,       # 232
    "Munchlax": 7289,      # 233
    "Ribombee": 7126,      # 234
    "Porygon-Z": 7040,     # 235
    "Shedinja": 6703,      # 236
    "Malamar": 6503,       # 237
    "Mightyena": 6459,     # 238
    "Heliolisk": 6386,     # 239
    "Froakie": 6293,       # 240
}

# ---------------------------------------------------------------------------
# Wikipedia pageviews — the MAINSTREAM-recognition signal (how many people
# actually look a Pokemon up). Cached into data/wiki_pageviews.json (en.wikipedia
# 12-month article views, matched to the specific Pokemon's article). Only ~50
# Pokemon are culturally famous enough to have their own standalone article; the
# rest rely on the poll. Regenerate with collectors/wikipopularity.py.
# ---------------------------------------------------------------------------
_WIKI_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "wiki_pageviews.json"
)
try:
    with open(_WIKI_PATH, encoding="utf-8") as _f:
        WIKI_VIEWS: Dict[str, int] = json.load(_f).get("views", {})
except (OSError, ValueError):
    WIKI_VIEWS = {}

# ---------------------------------------------------------------------------
# Two recognition signals -> a 0-10 score each, COMBINED by max(): a Pokemon is
# "famous" if it's a mainstream icon (high Wikipedia traffic) OR a fan favorite
# (high poll vote count). This is the key fix: Mewtwo has a mediocre poll rank
# but huge Wikipedia traffic, while Hydreigon has a decent poll rank but no
# article -> Mewtwo rightly outranks Hydreigon. These scores are a STARTING
# POINT, not gospel — pipeline/review_premium.py flags price-vs-recognition
# discrepancies for you to override via USER_OVERRIDES. 10.0 is reserved for
# those overrides.
# ---------------------------------------------------------------------------
POLL_FLOOR = 6.0      # poll/structural floor; unranked characters stay below this
WIKI_ONLY_CAP = 8.3   # cap for mainstream traffic the fan poll doesn't corroborate
                      # (keeps nostalgic Gen-1 traffic — Jynx, Butterfree, Cubone —
                      #  from cracking the top tier on Wikipedia views alone)


def _wiki_score(views: int) -> float:
    """Mainstream recognition from en.wikipedia 12-mo article views (log10 map:
    ~1k -> 6.5, ~100k -> 9.0, 1M -> 9.9). 10.0 is reserved for USER_OVERRIDES."""
    if views <= 0:
        return 0.0
    return max(6.5, min(9.9, math.log10(views) + 4.0))


def _poll_score(votes: int) -> float:
    """Fan recognition from the 2020 poll vote count (log10 map: winner ~140k ->
    9.3, rank-240 tail ~6k -> POLL_FLOOR)."""
    return max(POLL_FLOOR, min(9.3, 2.44 * math.log10(votes) - 3.27))


WIKI_SCORE: Dict[str, float] = {n: round(_wiki_score(v), 4) for n, v in WIKI_VIEWS.items()}
POLL_SCORE: Dict[str, float] = {n: round(_poll_score(v), 4) for n, v in POLL_VOTES.items()}


def _combined(name: str) -> Tuple[Optional[float], Optional[str]]:
    """Combine the wiki and poll signals a character has (or (None, None)).

    Both present  -> max() (each can lift the other; e.g. wiki rescues Mewtwo).
    Poll only     -> poll (the fan poll is Pokemon-specific, trustworthy alone).
    Wiki only     -> wiki capped at WIKI_ONLY_CAP (uncorroborated mainstream
                     traffic is general-culture noise, not card demand).
    """
    w = WIKI_SCORE.get(name)
    p = POLL_SCORE.get(name)
    if w is None and p is None:
        return None, None
    if w is not None and p is not None:
        # Reward corroboration: blend toward agreement so a high-wiki/low-poll
        # spike (Raichu: 228k views but poll rank 165) is tempered, while signals
        # that agree stay high. 0.7*stronger + 0.3*weaker.
        hi, lo = (w, p) if w >= p else (p, w)
        return 0.7 * hi + 0.3 * lo, ("wiki" if w >= p else "poll")
    if p is not None:
        return p, "poll"
    return min(w, WIKI_ONLY_CAP), "wiki"


def _variant_keys(base_name: str):
    """Yield lookup keys for a character, narrowing Mega/form variants to their
    base: 'Mega Charizard Y' -> 'Charizard X/Y/Z stripped' -> 'Charizard'."""
    yield base_name
    stem = base_name[5:] if base_name.startswith("Mega ") else base_name
    if stem != base_name:
        yield stem
    parts = stem.split()
    if len(parts) >= 2 and parts[-1] in {"X", "Y", "Z"}:
        yield " ".join(parts[:-1])


def popularity_prior(base_name: str,
                     backtest_safe: bool = False) -> Tuple[Optional[float], Optional[str]]:
    """Return (score, source) for a character, or (None, None) if not anchored.

    source is 'user' (hand-pinned override, wins), 'wiki' (mainstream Wikipedia
    traffic), or 'poll' (2020 fan poll). Mega/form variants inherit the base
    Pokemon. (None, None) -> caller falls back to the structural signal.

    ``backtest_safe=True`` uses the 2020 poll ONLY. USER_OVERRIDES were pinned
    after looking at current prices (review_premium) and the Wikipedia views
    cover 2025-06..2026-05, so both leak the future into historical backtest
    dates. The 2020 poll predates the whole price history.
    """
    if not base_name:
        return None, None
    keys = list(_variant_keys(base_name))
    if backtest_safe:
        for k in keys:
            if k in POLL_SCORE:
                return POLL_SCORE[k], "poll"
        return None, None
    for k in keys:
        if k in USER_OVERRIDES:
            return USER_OVERRIDES[k], "user"
    for k in keys:
        score, src = _combined(k)
        if score is not None:
            return round(score, 4), src
    return None, None
