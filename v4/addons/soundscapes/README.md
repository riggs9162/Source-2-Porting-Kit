# Soundscape port

Ports soundscapes from a Source 2 game into a Garry's Mod addon folder. It adds
**Sounds > Soundscape port** to the sidebar, and `soundscapes/list` and
`soundscapes/port` to the CLI and MCP.

One port writes three things:

| Output | Holds |
| --- | --- |
| `scripts/<script name>.txt` | The soundscapes as Source 1 rules |
| `lua/autorun/<lua name>.lua` | A `sound.Add` definition for every sound event they use |
| `sound/<sound folder>/...` | The sounds, as WAV files with their loop points |

## Use it

Paste a source into the **Source** field:

```text
vpk:S:/SteamLibrary/steamapps/common/Half-Life Alyx/game/hlvr/pak01_dir.vpk:scripts/
```

A pack's `_dir.vpk` path alone, an extracted game folder, and a single soundscape
script work too. The list fills as soon as the text is in. Soundscape scripts are
plain text even inside a pack, so listing them needs no other program. The sound
events load right after; the first time, Source 2 Viewer decompiles them, and the
result is kept until the pack changes.

Select a soundscape to see what it becomes: the sounds it plays, the ported script,
and anything that had to be left out. **Play soundscape** plays the loops at their
volumes and fires the random sounds on their timers. **Play sound**, or a double
click, plays one sound.

Tick the soundscapes to port, choose an output folder and a sound folder such as
`my_name/hlvr`, and press **Port**. The job appears under **Jobs & outputs**.
Soundscapes that a ticked one plays through `playsoundscape` are ported with it.

Sounds need ffmpeg, because Half-Life: Alyx stores most of them as MP3. Set its path
under **Settings**, or put it on PATH.

## What a soundscape becomes

Source 2 soundscapes use the same file format as Source 1 but name *sound events*
where Source 1 names files. Valve mix both kinds of rule in one script.

| Source 2 | Source 1 |
| --- | --- |
| An event that plays continuously | One `playlooping` per layer, because Source 1 cannot layer inside a rule |
| An event on a timer, or a burst | One `playrandom` with its takes in `rndwave` |
| A four-channel bed | Its front pair, unless folding is turned off |
| `playsoundscape` | Kept, with the nested soundscape ported too |
| A Source 1 rule | Kept as written |

Volumes multiply down the event graph and by the rule's own volume. Values a rule
sets on its event, such as `random_soundevent_01_timer_min`, replace the event's own.
Pitch and random ranges carry over: Source 2's `pitch = 1.0` with
`pitch_rand_min = -0.1` becomes `"pitch" "90,..."`.

A wave in a Source 1 rule is pointed at its ported copy when the game ships that
sound. A wave the game does not ship is a stock Half-Life 2 sound, so it keeps its
stock path.

A rule with an `origin` or a `position` always gets a `soundlevel`. Garry's Mod
plays a placed random sound everywhere when it has none.

Source 1 plays a sound file once per soundscape. Alyx often places one humming event
at several lights, so a repeat takes the event's next unused loop, and when none is
left it gets a copy of the file named `<name>_copy2.wav`. Soundscapes that play each
other share the limit while they run together, and the port does not look across them.

## Sound events

Every event a ported soundscape uses, and every child of it, is registered under its
own name with `sound.Add`, so anything that plays the event by name in Source 2 can
play it by name in Garry's Mod. Each definition carries the event's volume, pitch and
random ranges, its files, and a sound level.

An event that plays one sound or picks between takes is defined exactly. An event
that plays several layers at once cannot be one Source 1 sound: its definition plays
the first layer, and the report marks it `combined`. When the layers are the event's
own files, each also gets an exact `<event>.Layer1`, `.Layer2` definition, so Lua can
start them together.

The sound level comes from the event's falloff distances. Source 2 holds full volume
to `volume_falloff_min` and reaches silence at `volume_falloff_max`; Source 1 halves
the volume each time the distance doubles. The level puts Source 1's half volume
midway between the two distances. Beds that are heard everywhere get level 0.

## Sounds

`sounds/ambient/wind.vsnd` becomes `sound/<sound folder>/ambient/wind.wav`: the
`sounds/` folder is replaced by your folder and the rest of the path is kept.

Every sound is written as 16-bit PCM. MP3 audio is cut to the exact sample count the
sound's header gives, and loop points come from the header too, as a cue point and a
sampler chunk. Source 1 only loops a WAV that has them. A looping sound that only
random rules use is written without loop points, because a looping sound in a
`playrandom` never stops.

## Loading the script

Name the script `soundscapes_<map name>` and that map loads it by itself. For any
other name, **Merge into Garry's Mod's soundscape manifest** writes the game's own
manifest with the script added. Only one addon can replace the manifest at a time.

Running a port again replaces the script and the Lua file of the same names, so use
a different name for each set you want to keep.

## What does not carry over

Source 2 mixes sound at run time: volume curves driven by game state, filtering,
occlusion, moving sources and doppler. Source 1 soundscapes have none of that. The
port gives the bed and the placement, so expect to set some levels by ear.

Whether Garry's Mod reads `origin` in a soundscape rule has not been checked in the
game. The Valve wiki lists it for Left 4 Dead and later.

## Agents

```json
{
  "source": "vpk:S:/SteamLibrary/steamapps/common/Half-Life Alyx/game/hlvr/pak01_dir.vpk:scripts/",
  "soundscapes": "a2_hideout_*",
  "output": "A:/ports/hideout",
  "sound_prefix": "my_name/hlvr",
  "script_name": "soundscapes_my_map"
}
```

```powershell
& .\v4\Backport.cmd run soundscapes/list --source "vpk:...pak01_dir.vpk:scripts/"
& .\v4\Backport.cmd submit soundscapes/port --json-file inputs.json
```

`soundscapes` takes one name per line and `*` as a wildcard. `events` adds sound
events no picked soundscape uses. The job's `soundscape-report.json` lists every
soundscape, definition, sound and note.

## Layout

| Folder | Holds |
| --- | --- |
| `Formats/` | KeyValues, KV3 text and pack reading |
| `Source/` | Opening a pack or folder, and finding ffmpeg and Source 2 Viewer |
| `Events/` | Sound events, their `base` chains, and resolving one to the files it plays |
| `Port/` | Soundscape conversion and the Lua writer |
| `Audio/` | Reading compiled sounds and writing looped WAV files |
| `Tools/`, `UI/` | The two operations, the desktop page and its player |
