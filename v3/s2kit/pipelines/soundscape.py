"""
Soundscapes: read Source 2's, write Source 1's.

Half-Life: Alyx keeps its soundscapes in `scripts/soundscapes_*.txt` - the same
KeyValues container Source 1 uses, but with a different body. Where Source 1
names a `.wav` directly inside a `playlooping` or `playrandom` rule, Source 2
writes a `playevent` naming a *sound event*, and that event lives in a KV3
`.vsndevts` file which eventually resolves to one or more `.vsnd` files.

So porting a soundscape is three jobs:

1. parse the soundscape (a KeyValues file either way);
2. resolve each `playevent` through the sound-event table, following `base`
   inheritance, down to real audio files and their volumes;
3. emit Source 1 rules naming those files.

Two facts from Valve's own shipped data shape this module:

* **The two formats are mixed inside one file.** `soundscapes_a2.txt` holds 266
  `playevent` blocks *and* 40 Source 1 rules. Anything already in Source 1 form
  is passed through untouched rather than round-tripped through the converter.
* **Some of a soundscape's meaning is in its comments.** Valve comment out
  `playevent` blocks in place rather than deleting them, so a parser that does
  not understand `//` will emit sounds the game never plays.

Nothing here imports Flet, so it is testable on its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Optional, Union

#: Rule blocks Source 1 understands inside a soundscape.
SOURCE1_RULES = ("playlooping", "playrandom", "playsoundscape")

#: Scalar keys Source 1 allows directly on a soundscape.
SOURCE1_SCALARS = ("dsp", "dsp_volume", "fadetime", "soundmixer")

#: Source 2's marker for a sound-event reference inside a soundscape.
SOURCE2_RULE = "playevent"


class SoundscapeError(RuntimeError):
    """Raised when a file cannot be parsed as KeyValues."""


KVValue = Union[str, "KVBlock"]


@dataclass
class KVBlock:
    """An ordered, duplicate-tolerant KeyValues block.

    Duplicates are the point: a soundscape is mostly a run of blocks that all
    share the key `playevent`, so a plain dict would silently keep only the
    last one. Order is preserved because a soundscape reads top to bottom.
    """

    pairs: list[tuple[str, KVValue]] = field(default_factory=list)

    def __iter__(self) -> Iterator[tuple[str, KVValue]]:
        return iter(self.pairs)

    def __len__(self) -> int:
        return len(self.pairs)

    def add(self, key: str, value: KVValue) -> None:
        self.pairs.append((key, value))

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """The first scalar stored under `key`, matched case-insensitively."""
        wanted = key.lower()
        for name, value in self.pairs:
            if name.lower() == wanted and isinstance(value, str):
                return value
        return default

    def block(self, key: str) -> Optional["KVBlock"]:
        wanted = key.lower()
        for name, value in self.pairs:
            if name.lower() == wanted and isinstance(value, KVBlock):
                return value
        return None

    def all(self, key: str) -> list[KVValue]:
        wanted = key.lower()
        return [value for name, value in self.pairs if name.lower() == wanted]

    def has(self, key: str) -> bool:
        wanted = key.lower()
        return any(name.lower() == wanted for name, _ in self.pairs)

    def scalars(self) -> dict[str, str]:
        """Flat view of the scalar pairs, the last of a repeated key winning."""
        return {name: value for name, value in self.pairs
                if isinstance(value, str)}


_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([^\s{}"]+)|([{}])')


def _strip_comment(line: str) -> str:
    """Remove a `//` comment, ignoring one that sits inside a quoted string."""
    in_quote = False
    index = 0
    while index < len(line):
        char = line[index]
        if char == "\\" and in_quote:
            index += 2
            continue
        if char == '"':
            in_quote = not in_quote
        elif char == "/" and not in_quote and line[index + 1:index + 2] == "/":
            return line[:index]
        index += 1
    return line


def _tokenise(text: str) -> Iterator[tuple[str, str]]:
    """Yield (kind, text) over a KeyValues document, dropping comments.

    Comments are stripped line by line rather than with one regex over the
    whole document, so a `//` inside a quoted path cannot swallow the rest of
    the file.
    """
    for raw_line in text.splitlines():
        line = _strip_comment(raw_line)
        if not line.strip():
            continue
        for match in _TOKEN.finditer(line):
            quoted, bare, brace = match.group(1), match.group(2), match.group(3)
            if brace:
                yield ("brace", brace)
            elif quoted is not None:
                yield ("string", quoted.replace('\\"', '"'))
            elif bare:
                yield ("string", bare)


def parse_kv(text: str) -> KVBlock:
    """Parse a KeyValues document into a root block.

    Keys may be bare: Valve's own `soundscapes_manifest.txt` opens with an
    unquoted key, so the tokeniser treats quoted and bare words alike.
    """
    root = KVBlock()
    stack: list[KVBlock] = [root]
    pending: Optional[str] = None

    for kind, value in _tokenise(text):
        if kind == "brace":
            if value == "{":
                if pending is None:
                    raise SoundscapeError("'{' with no key before it")
                child = KVBlock()
                stack[-1].add(pending, child)
                stack.append(child)
                pending = None
            else:
                if len(stack) == 1:
                    raise SoundscapeError("'}' without a matching '{'")
                stack.pop()
            continue
        if pending is None:
            pending = value
        else:
            stack[-1].add(pending, value)
            pending = None

    if len(stack) != 1:
        raise SoundscapeError("unbalanced braces - a block was never closed")
    return root


def write_kv(block: KVBlock, *, indent: int = 0) -> str:
    """Emit KeyValues in Valve's house style: tabs, quoted keys and values."""
    lines: list[str] = []
    pad = "\t" * indent
    for key, value in block.pairs:
        if isinstance(value, KVBlock):
            lines.append(f'{pad}"{key}"')
            lines.append(f"{pad}{{")
            body = write_kv(value, indent=indent + 1)
            if body:
                lines.append(body)
            lines.append(f"{pad}}}")
        else:
            lines.append(f'{pad}"{key}"\t"{value}"')
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Sound events (.vsndevts - KV3 text)
# ----------------------------------------------------------------------

EventFields = dict[str, object]

_KV3_HEADER = re.compile(r"^\s*<!--.*?-->\s*", re.DOTALL)
_KV3_TOKEN = re.compile(
    r'"(?P<str>(?:[^"\\]|\\.)*)"'
    r'|(?P<punct>[{}\[\]=,])'
    r'|(?P<bare>[^\s{}\[\]=,"]+)'
)


class _Kv3Reader:
    """A reader for the small KV3 subset `.vsndevts` files actually use.

    Valve's 129 shipped sound-event files use exactly three value kinds -
    strings, numbers/booleans, and arrays of strings - with no nested objects
    below an event, no comments and no multi-line strings. This reader covers
    that subset and raises rather than guessing on anything else, so a file
    using more is a loud failure instead of silently missing sounds.
    """

    def __init__(self, text: str) -> None:
        body = _KV3_HEADER.sub("", text, count=1)
        self.tokens = [
            (m.lastgroup, m.group(m.lastgroup))
            for m in _KV3_TOKEN.finditer(body)
        ]
        self.index = 0

    def peek(self) -> Optional[tuple[str, str]]:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def next(self) -> tuple[str, str]:
        if self.index >= len(self.tokens):
            raise SoundscapeError("unexpected end of KV3 document")
        token = self.tokens[self.index]
        self.index += 1
        return token

    def expect(self, text: str) -> None:
        kind, value = self.next()
        if value != text:
            raise SoundscapeError(f"expected {text!r} in KV3, got {value!r}")

    def read_document(self) -> dict[str, EventFields]:
        self.expect("{")
        events: dict[str, EventFields] = {}
        while True:
            token = self.peek()
            if token is None:
                raise SoundscapeError("KV3 document was never closed")
            if token[1] == "}":
                self.next()
                break
            _kind, name = self.next()
            self.expect("=")
            events[name] = self.read_object()
        return events

    def read_object(self) -> EventFields:
        self.expect("{")
        fields: EventFields = {}
        while True:
            token = self.peek()
            if token is None:
                raise SoundscapeError("KV3 object was never closed")
            if token[1] == "}":
                self.next()
                break
            if token[1] == ",":
                self.next()
                continue
            _kind, key = self.next()
            self.expect("=")
            fields[key] = self.read_value()
        return fields

    def read_value(self) -> object:
        kind, value = self.next()
        if value == "[":
            # Elements are read recursively: Valve stores speaker layouts as
            # arrays of [x, y, z] arrays, so a flat token scan derails there.
            items: list[object] = []
            while True:
                token = self.peek()
                if token is None:
                    raise SoundscapeError("KV3 array was never closed")
                if token[1] == "]":
                    self.next()
                    break
                if token[1] == ",":
                    self.next()
                    continue
                items.append(self.read_value())
            return items
        if value == "{":
            self.index -= 1
            return self.read_object()
        return value


def parse_vsndevts(text: str) -> dict[str, EventFields]:
    """Parse one `.vsndevts` file into `{event name: fields}`."""
    return _Kv3Reader(text).read_document()


@dataclass
class EventTable:
    """Every sound event the porter can see, keyed by name.

    Sound events inherit: roughly a fifth of Valve's carry a `base` naming
    another event, and the child usually overrides only the file list or a
    rotation angle. Resolving that chain is not optional - skip it and a
    surround channel loses the volume and file list it was relying on.
    """

    events: dict[str, EventFields] = field(default_factory=dict)
    #: Which file each event came from, for reporting.
    origins: dict[str, str] = field(default_factory=dict)

    def add_file(self, name: str, text: str) -> int:
        parsed = parse_vsndevts(text)
        for event_name, fields in parsed.items():
            self.events[event_name] = fields
            self.origins[event_name] = name
        return len(parsed)

    def __contains__(self, name: str) -> bool:
        return name in self.events

    def __len__(self) -> int:
        return len(self.events)

    def resolve(self, name: str) -> Optional[EventFields]:
        """The event's fields with its `base` chain merged in, child first.

        Returns None for an unknown event. A cyclic `base` chain resolves to
        what it had accumulated rather than hanging.
        """
        if name not in self.events:
            return None
        merged: EventFields = {}
        seen: set[str] = set()
        current: Optional[str] = name
        while current and current not in seen:
            seen.add(current)
            fields = self.events.get(current)
            if fields is None:
                break
            for key, value in fields.items():
                # The child was visited first, so it wins.
                merged.setdefault(key, value)
            base = fields.get("base")
            current = base if isinstance(base, str) else None
        merged.pop("base", None)
        return merged

    def base_chain(self, name: str) -> list[str]:
        """The inheritance chain from `name` upwards, for diagnostics."""
        chain: list[str] = []
        seen: set[str] = set()
        current: Optional[str] = name
        while current and current not in seen and current in self.events:
            seen.add(current)
            chain.append(current)
            base = self.events[current].get("base")
            current = base if isinstance(base, str) else None
        return chain


# ----------------------------------------------------------------------
# Resolving an event down to audio
# ----------------------------------------------------------------------

#: Source 2 keeps audio under `sounds/`; Source 1's sound root is `sound/`, and
#: a `wave` is written relative to it. Carrying the Source 2 prefix through
#: unchanged asks the engine for `sound/sounds/...`, which silently plays
#: nothing.
SOURCE2_SOUND_ROOT = "sounds/"

#: Event types whose children are alternatives to pick between rather than
#: layers to play together.
RANDOM_TYPE_MARKERS = ("ambient_rand", "ambient_burst", "_rand_child")

#: A quad ambience is four events at 45/135/225/315 degrees. Source 1 has no
#: such thing, so only the front pair survives the port.
QUAD_TYPE_MARKER = "multi_quad"

#: How Valve name the rear channels of a surround bed. Source 1 soundscapes are
#: stereo, so these are dropped rather than played as extra front layers.
SURROUND_SUFFIXES = ("_ls", "_rs", "_sl", "_sr")


@dataclass
class Layer:
    """One audio file that will become one `wave` line."""

    path: str
    volume: float = 1.0
    rotation: Optional[float] = None

    def wave(self, extension: str = ".wav", prefix: str = "") -> str:
        """The path as Source 1 wants it: relative to `sound/`, re-extensioned.

        `prefix` namespaces the result. Half-Life 2 already owns
        `sound/ambient/`, and Alyx reuses those names, so shipping an addon
        without a prefix overwrites the base game's ambience for everyone.
        """
        path = self.path.replace("\\", "/")
        while "//" in path:
            path = path.replace("//", "/")
        path = path.lstrip("/")
        lowered = path.lower()
        if lowered.startswith(SOURCE2_SOUND_ROOT):
            path = path[len(SOURCE2_SOUND_ROOT):]
        stem = path.rsplit(".", 1)[0] if "." in path.rsplit("/", 1)[-1] else path
        namespace = prefix.replace("\\", "/").strip("/")
        if namespace:
            stem = f"{namespace}/{stem}"
        return stem + extension


@dataclass
class ResolvedEvent:
    """What a `playevent` actually plays, once the event graph is walked."""

    name: str
    #: Files that play together. Source 1 cannot layer inside one rule, so each
    #: becomes its own `playlooping`.
    stems: list[Layer] = field(default_factory=list)
    #: Files chosen between, which become one `rndwave` block.
    alternatives: list[Layer] = field(default_factory=list)
    looping: bool = True
    time_range: Optional[tuple[float, float]] = None
    volume: float = 1.0
    #: Events referenced but not found in any `.vsndevts`.
    missing: list[str] = field(default_factory=list)
    #: Surround channels and other detail dropped on the way to Source 1.
    dropped: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.stems and not self.alternatives


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def _numbered(fields: EventFields, prefix: str) -> list[tuple[str, object]]:
    """Fields like `vsnd_file_01`, in numeric order, blanks dropped."""
    found = []
    for key, value in fields.items():
        if not key.startswith(prefix):
            continue
        suffix = key[len(prefix):]
        if suffix.isdigit():
            found.append((int(suffix), key, value))
    return [(key, value) for _n, key, value in sorted(found)
            if isinstance(value, str) and value.strip()]


#: Keys whose value names a child event that *plays as part of this one*.
#: Taken from a census of Valve's 12,521 shipped events rather than guessed.
#: Deliberately excluded are references that exist for mixing rather than
#: playback - `limiter_event_name` (ducking), `metadata`, `soundname_string` -
#: because following those pulls unrelated audio into an ambience bed.
CHILD_EVENT_KEYS = (
    "soundevents", "soundevent_name", "soundevent", "soundevent_core",
    "child_soundevent_name", "child_soundevent", "oneshot_child", "event_name",
)


def _child_events(fields: EventFields) -> list[str]:
    """Named child events, in order, excluding the numeric-suffix parameters.

    Three spellings are in use: `soundevent_01`-style numbering, a
    `random_soundevent_01_name` for random pickers, and the plural
    `soundevents`, which Valve write as either an array or a lone string.
    """
    numbered: list[tuple[int, str]] = []
    plain: list[str] = []
    for key, value in fields.items():
        if key.startswith("soundevent_") and key[len("soundevent_"):].isdigit():
            if isinstance(value, str) and value.strip():
                numbered.append((int(key[len("soundevent_"):]), value))
            continue
        if key.startswith("random_soundevent_") and key.endswith("_name"):
            middle = key[len("random_soundevent_"):-len("_name")]
            if middle.isdigit() and isinstance(value, str) and value.strip():
                numbered.append((int(middle), value))
            continue
        if key in CHILD_EVENT_KEYS:
            entries = value if isinstance(value, list) else [value]
            plain.extend(item for item in entries
                         if isinstance(item, str) and item.strip())
    return [name for _n, name in sorted(numbered)] + plain


def _is_random(fields: EventFields) -> bool:
    kind = str(fields.get("type", "")).lower()
    if any(marker in kind for marker in RANDOM_TYPE_MARKERS):
        return True
    return any(key.startswith("random_soundevent_") and key.endswith("_timer_min")
               for key in fields)


def _timer_range(fields: EventFields) -> Optional[tuple[float, float]]:
    """The retrigger window of a random ambience, in seconds."""
    low = high = None
    for key, value in fields.items():
        if key.startswith("random_soundevent_") and key.endswith("_timer_min"):
            low = _as_float(value, 0.0)
        elif key.startswith("random_soundevent_") and key.endswith("_timer_max"):
            high = _as_float(value, 0.0)
    if low is None and high is None:
        low = _as_float(fields.get("rand_delay_min"), 0.0)
        high = _as_float(fields.get("rand_delay_max"), 0.0)
        if low <= 0 and high <= 0:
            return None
    low = low or 0.0
    high = high or low
    if high < low:
        low, high = high, low
    return (low, high)


def resolve_event(table: EventTable, name: str, *, quad_fold: bool = True,
                  _depth: int = 0, _seen: Optional[set] = None) -> ResolvedEvent:
    """Walk an event down to the audio files it plays.

    Volumes multiply down the graph, because that is how the layers combine:
    an event at 0.9 whose stem is tagged 0.4 is playing that stem at 0.36.
    """
    resolved = ResolvedEvent(name=name)
    seen = _seen if _seen is not None else set()
    if name in seen or _depth > 8:
        return resolved
    seen.add(name)

    fields = table.resolve(name)
    if fields is None:
        resolved.missing.append(name)
        return resolved

    resolved.volume = _as_float(fields.get("volume"), 1.0) or 1.0
    random_event = _is_random(fields)
    resolved.looping = not random_event
    resolved.time_range = _timer_range(fields) if random_event else None
    rotation = _as_float(fields.get("rotation_angle"), 0.0) or None

    kind = str(fields.get("type", "")).lower()
    children = _child_events(fields)
    if quad_fold:
        # Fold on the channel suffixes rather than the event type. Valve build
        # surround beds under both `..._multi_quad` and plain `..._multi`, so
        # trusting the type leaves the rear pair playing as extra front loops -
        # audibly wrong, and twice the sounds to ship.
        surrounds = [child for child in children
                     if child.lower().endswith(SURROUND_SUFFIXES)]
        if surrounds and len(surrounds) < len(children):
            resolved.dropped.append(
                f"{name}: surround channels dropped, kept the front pair - "
                + ", ".join(surrounds))
            children = [c for c in children if c not in surrounds]
        elif QUAD_TYPE_MARKER in kind and len(children) > 2:
            resolved.dropped.append(
                f"{name}: quad ambience folded to its front pair, dropped "
                + ", ".join(children[2:]))
            children = children[:2]

    for child in children:
        inner = resolve_event(table, child, quad_fold=quad_fold,
                              _depth=_depth + 1, _seen=seen)
        resolved.missing.extend(inner.missing)
        resolved.dropped.extend(inner.dropped)
        bucket = resolved.alternatives if random_event else resolved.stems
        for layer in inner.stems + inner.alternatives:
            bucket.append(Layer(path=layer.path,
                                volume=layer.volume * resolved.volume,
                                rotation=layer.rotation))
        if inner.time_range and not resolved.time_range:
            resolved.time_range = inner.time_range

    # Numbered files are layers, each with its own trim; the `vsnd_files` array
    # is a set of takes to choose between.
    volumes = dict(_numbered(fields, "vsnd_vol_"))
    for key, path in _numbered(fields, "vsnd_file_"):
        trim = _as_float(volumes.get("vsnd_vol_" + key[len("vsnd_file_"):]), 1.0)
        resolved.stems.append(Layer(path=str(path),
                                    volume=resolved.volume * (trim or 1.0),
                                    rotation=rotation))

    # `vsnd_files` is usually an array, but Valve write a lone take as a bare
    # string. Handling only the array form loses the sound without a trace.
    array = fields.get("vsnd_files")
    if isinstance(array, (list, str)):
        entries = [array] if isinstance(array, str) else array
        takes = [str(item) for item in entries
                 if isinstance(item, str) and item.strip()]
        target = resolved.alternatives if len(takes) > 1 or random_event \
            else resolved.stems
        for path in takes:
            target.append(Layer(path=path, volume=resolved.volume,
                                rotation=rotation))

    if resolved.alternatives and not resolved.looping and not resolved.time_range:
        resolved.time_range = (4.0, 8.0)
    return resolved


# ----------------------------------------------------------------------
# Emitting Source 1
# ----------------------------------------------------------------------


@dataclass
class ConvertOptions:
    """How to shape the Source 1 side of the port."""

    #: Extension the ported audio will carry. Looping ambience wants `.wav`,
    #: because Source only loops a wave that carries a `smpl` chunk.
    extension: str = ".wav"
    #: Written on every rule when set. Left empty by default: a soundscape bed
    #: is 2D, and giving it a soundlevel makes it fall off with distance.
    soundlevel: str = ""
    #: Fold a quad ambience to its front pair instead of dropping it.
    quad_fold: bool = True
    #: Cap the layers one event may become. 0 keeps them all.
    max_stems: int = 0
    #: Keep rules that are already Source 1 rather than dropping them.
    keep_source1: bool = True
    #: Skip Valve's explicit silence placeholder.
    drop_null_sounds: bool = True
    #: Folder under `sound/` that ported audio is written beneath, so it cannot
    #: overwrite Half-Life 2's - Alyx reuses many of the same names.
    wave_prefix: str = ""
    #: Prepended to every soundscape name. Soundscape names are global in
    #: Source, so an unprefixed port silently replaces a stock one.
    name_prefix: str = ""
    #: Port only these soundscapes. None means all of them.
    only: Optional[set] = None


@dataclass
class ConvertResult:
    """The ported soundscape file, and what porting it needs and lost."""

    block: KVBlock
    soundscapes: int = 0
    rules: int = 0
    #: Every Source 2 path the port depends on, for the audio stage to extract.
    sources: list[str] = field(default_factory=list)
    #: Of those, the ones a `playlooping` rule uses. Source only loops a WAV
    #: carrying a `smpl` chunk, so these need transcoding and a loop point
    #: while a one-shot can stay in whatever format it decoded to.
    looping_sources: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return write_kv(self.block) + "\n"


def _clamp_volume(value: float) -> str:
    return f"{max(0.0, min(1.0, value)):.2f}"


def _rule_common(rule: KVBlock, playevent: KVBlock,
                 options: ConvertOptions) -> None:
    """Copy the placement keys Source 1 shares with Source 2."""
    position = playevent.get("position")
    if position:
        rule.add("position", position)
    origin = playevent.get("origin")
    if origin:
        rule.add("origin", origin)
    if options.soundlevel:
        rule.add("soundlevel", options.soundlevel)


def convert_playevent(playevent: KVBlock, table: EventTable,
                      options: ConvertOptions) -> tuple[list[tuple[str, KVBlock]],
                                                        list[str], list[str],
                                                        set[str]]:
    """One `playevent` becomes zero or more Source 1 rules.

    Returns (rules, source paths, warnings). A looping event with three layered
    stems becomes three `playlooping` rules, because Source 1 cannot layer
    inside one rule; a random one becomes a single `playrandom` whose takes sit
    in an `rndwave` block.
    """
    event_name = playevent.get("event", "")
    if not event_name:
        return [], [], ["a playevent with no event name was skipped"], set()

    resolved = resolve_event(table, event_name, quad_fold=options.quad_fold)
    warnings = [f"{event_name}: not found in any .vsndevts"
                for _ in resolved.missing[:1]]
    warnings.extend(resolved.dropped)

    scale = _as_float(playevent.get("volume"), 1.0)
    if scale <= 0:
        scale = 1.0

    def usable(layers: list[Layer]) -> list[Layer]:
        kept = [l for l in layers
                if not (options.drop_null_sounds and "null" in l.path.lower()
                        and l.path.lower().endswith("null.vsnd"))]
        return kept[:options.max_stems] if options.max_stems else kept

    rules: list[tuple[str, KVBlock]] = []
    sources: list[str] = []

    if resolved.looping:
        for layer in usable(resolved.stems + resolved.alternatives):
            rule = KVBlock()
            rule.add("volume", _clamp_volume(layer.volume * scale))
            _rule_common(rule, playevent, options)
            rule.add("wave", layer.wave(options.extension,
                                        options.wave_prefix))
            rules.append(("playlooping", rule))
            sources.append(layer.path)
        return rules, sources, warnings, set(sources)

    takes = usable(resolved.alternatives + resolved.stems)
    if not takes:
        return [], [], warnings, set()
    rule = KVBlock()
    loudest = max(layer.volume for layer in takes)
    rule.add("volume", _clamp_volume(loudest * scale))
    low, high = resolved.time_range or (4.0, 8.0)
    rule.add("time", f"{low:g},{high:g}")
    _rule_common(rule, playevent, options)
    waves = KVBlock()
    for layer in takes:
        waves.add("wave", layer.wave(options.extension,
                                     options.wave_prefix))
        sources.append(layer.path)
    rule.add("rndwave", waves)
    rules.append(("playrandom", rule))
    return rules, sources, warnings, set()


def convert_soundscape(name: str, body: KVBlock, table: EventTable,
                       options: ConvertOptions) -> tuple[KVBlock, list[str],
                                                         list[str], set[str]]:
    """Port one named soundscape, keeping any Source 1 rules it already has."""
    ported = KVBlock()
    sources: list[str] = []
    warnings: list[str] = []
    looping: set[str] = set()

    for key, value in body:
        lowered = key.lower()
        if lowered == SOURCE2_RULE and isinstance(value, KVBlock):
            rules, used, notes, loops = convert_playevent(value, table, options)
            looping |= loops
            for rule_name, rule in rules:
                ported.add(rule_name, rule)
            sources.extend(used)
            warnings.extend(f"{name}: {note}" for note in notes)
        elif lowered in SOURCE1_RULES and isinstance(value, KVBlock):
            if options.keep_source1:
                ported.add(key, value)
        elif isinstance(value, str):
            ported.add(key, value)
        elif options.keep_source1 and isinstance(value, KVBlock):
            ported.add(key, value)

    if not len(ported):
        warnings.append(f"{name}: nothing to play after porting")
    return ported, sources, warnings, looping


def convert_file(text: str, table: EventTable,
                 options: Optional[ConvertOptions] = None) -> ConvertResult:
    """Port a whole `soundscapes_*.txt`, soundscape by soundscape."""
    options = options or ConvertOptions()
    root = parse_kv(text)
    result = ConvertResult(block=KVBlock())

    for name, body in root:
        if not isinstance(body, KVBlock):
            continue
        if options.only is not None and name not in options.only:
            continue
        ported, sources, warnings, looping = convert_soundscape(
            name, body, table, options)
        result.looping_sources |= looping
        result.block.add(options.name_prefix + name, ported)
        result.soundscapes += 1
        result.rules += len(ported)
        result.sources.extend(sources)
        result.warnings.extend(warnings)

    seen: set[str] = set()
    unique: list[str] = []
    for path in result.sources:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    result.sources = unique
    return result


# ----------------------------------------------------------------------
# Browsing what a game has
# ----------------------------------------------------------------------


@dataclass
class SoundscapeEntry:
    """One soundscape found in a file, summarised for picking from a list."""

    name: str
    #: File it was found in, as a display name.
    source: str
    #: `playevent` blocks - the Source 2 half.
    playevents: int = 0
    #: Rules that are already Source 1 and will pass straight through.
    source1_rules: int = 0
    #: Sound events it names, in order, duplicates removed.
    events: list[str] = field(default_factory=list)

    @property
    def portable(self) -> bool:
        """Whether there is anything here worth porting."""
        return bool(self.playevents or self.source1_rules)

    @property
    def kind(self) -> str:
        if self.playevents and self.source1_rules:
            return "mixed"
        if self.playevents:
            return "source2"
        return "source1"


def index_soundscapes(text: str, source: str = "") -> list[SoundscapeEntry]:
    """Summarise every soundscape in one file, without resolving any audio.

    This is what fills the picker, so it has to stay cheap: no event table is
    consulted and nothing is converted.
    """
    entries: list[SoundscapeEntry] = []
    for name, body in parse_kv(text):
        if not isinstance(body, KVBlock):
            continue
        entry = SoundscapeEntry(name=name, source=source)
        seen: set[str] = set()
        for key, value in body:
            lowered = key.lower()
            if lowered == SOURCE2_RULE and isinstance(value, KVBlock):
                entry.playevents += 1
                event = value.get("event")
                if event and event not in seen:
                    seen.add(event)
                    entry.events.append(event)
            elif lowered in SOURCE1_RULES and isinstance(value, KVBlock):
                entry.source1_rules += 1
        entries.append(entry)
    return entries


def select_soundscapes(text: str, names: Iterable[str]) -> KVBlock:
    """A new root holding only the named soundscapes, in the file's own order."""
    wanted = set(names)
    picked = KVBlock()
    for name, body in parse_kv(text):
        if name in wanted and isinstance(body, KVBlock):
            picked.add(name, body)
    return picked
