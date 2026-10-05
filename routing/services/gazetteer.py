"""Offline US place -> coordinate lookup built from the Census Gazetteer.

Geocoding ~3,900 distinct city/state pairs through a rate-limited web service
would take over an hour. The Census Gazetteer ships the same information as two
small bundled files, which resolves ~94% of our cities instantly and offline.
Whatever is left falls back to Nominatim.

Source: https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.html
(public domain, US Census Bureau)
"""

import csv
import gzip
import re
import unicodedata
from pathlib import Path

from django.conf import settings

# Census names carry a legal-entity suffix ("Tomah city", "Berlin town") that
# never appears in the OPIS address data, so it is stripped before indexing.
ENTITY_SUFFIX = re.compile(
    r'\s+('
    r'CDP|city|town|village|borough|municipality|township|charter township'
    r'|plantation|gore|district|comunidad|zona urbana|County'
    r')(\s+\(.*\))?$',
    re.IGNORECASE,
)

GAZETTEER_FILES = (
    '2024_Gaz_place_national.txt.gz',
    '2024_Gaz_cousubs_national.txt.gz',
)

# Characters the OPIS city names drop but the Census names keep ("Oneill" vs
# "O'Neill", "Coeur Dalene" vs "Coeur d'Alene"), plus the reverse. Matching on a
# punctuation-free, accent-free key makes both spellings meet.
_PUNCTUATION_CHARS = re.compile(r"[^A-Z0-9\s]+")
_WHITESPACE = re.compile(r"\s+")

_index = None


def _data_dir():
    return Path(getattr(settings, 'BASE_DIR')) / 'data'


def normalise(name):
    """Fold a place name to a punctuation- and accent-free comparison key."""
    folded = unicodedata.normalize('NFKD', name)
    folded = ''.join(ch for ch in folded if not unicodedata.combining(ch))
    # Punctuation is dropped, not spaced out, so "Oneill" and "O'Neill" fold
    # to the same key; whitespace is then collapsed.
    stripped = _PUNCTUATION_CHARS.sub('', folded.upper())
    return _WHITESPACE.sub(' ', stripped).strip()


def _read_file(path):
    """Yield ``((name, state), (lat, lon))`` from one tab-separated Gazetteer file."""
    with gzip.open(path, mode='rt', encoding='latin-1') as handle:
        reader = csv.reader(handle, delimiter='\t')
        header = [column.strip() for column in next(reader)]
        try:
            # The real file pads the INTPTLONG header with ~100 trailing spaces,
            # so every column name must be stripped before being looked up.
            idx = {key: header.index(key) for key in ('USPS', 'NAME', 'INTPTLAT', 'INTPTLONG')}
        except ValueError:  # pragma: no cover - malformed file
            return

        for row in reader:
            if len(row) <= idx['INTPTLONG']:
                continue
            name = normalise(ENTITY_SUFFIX.sub('', row[idx['NAME']].strip()))
            state = row[idx['USPS']].strip().upper()
            try:
                lat = float(row[idx['INTPTLAT']])
                lon = float(row[idx['INTPTLONG']].strip())
            except ValueError:
                continue
            yield (name, state), (lat, lon)


def get_index():
    """Load and memoise the ``(city, state) -> (lat, lon)`` index."""
    global _index
    if _index is not None:
        return _index

    index = {}
    for filename in GAZETTEER_FILES:
        path = _data_dir() / filename
        if not path.exists():
            continue
        for key, point in _read_file(path):
            # Incorporated places win over county subdivisions of the same name,
            # hence setdefault and the file ordering above.
            index.setdefault(key, point)

    _index = index
    return _index


def lookup(city, state):
    """Return ``(lat, lon)`` for a US city/state, or ``None`` if unknown."""
    if not city or not state:
        return None
    return get_index().get((normalise(city), state.strip().upper()))
