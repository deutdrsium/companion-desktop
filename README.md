# companion-desktop

Local desktop runtime for an AstrBot companion character:

- a procedural animation "cerebellum" that drives a Live2D model through VTube Studio
- a local voice timeline with lip-sync, subtitles and interruption
- desktop observation with explicit consent
- game executors built on the public Neuro game protocol (first target: Stardew Valley via the Neuro mod)
- the AstrBot desktop-access plugin that connects all of the above to the companion plugin

Status: early development. Work packages P0–P9 are tracked in issues/PRs.

## Desktop client (U1)

`src/companion_desktop/` contains the panel UI (`ui/`) and the client shell (`client/`):
launch card, server link with automatic reconnect, an adapter for the current AstrBot
desktop HTTP API, local voice playback, the loopback subtitle feed for a VTube Studio
Web Item (1024 × 200) and the tray.

```
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[test]"
copy config\client.example.json config\client.json   # then fill in; config/*.json is git-ignored
.venv\Scripts\pythonw -m companion_desktop
```

The connection key is read from the OS credential store (`account.keyring_service` /
`account.username`). Options: `--config PATH`, `--no-helpers`, `--steam-autostart`.

With `server.device_session` enabled, the Connection page shows the host grants for
chat and avatar control. The host owns the device binding and its permissions. Use
Reconnect after a host-side grant change to register a fresh session.

The wheel includes `astrbot_plugin_desktop_access.session` and the shared session
contract for host integrations that install the wheel into their runtime.

### Shared media

Set `shared_media_enabled` to `true` in local client config alongside
`server.device_session`. The host must also enable shared media and grant `speak`.
The controller then exposes `Controller.shared_media`. A user action must call
`start(source, kind)` for each shared session. `stop()` ends sharing. Playback
controls continue to belong to the selected player.

- `sources` and `sources_changed` provide local source choices. Call `refresh_sources()`
  to enumerate Windows media sessions and `select_system(id)` to follow one.
- `load_local(path, timed_text=None)` loads a local file. Use
  `local_player.player` for play, pause, volume and `setVideoOutput(widget)`.
  Call `seek_local(position_ms)` for seeks and `set_timed_text(path_or_none)` to select
  an SRT or LRC for the current item from either source.
- `start("local_player" | "system_media", "music" | "movie")` selects a source and kind.
  `current_item` and `item_changed` expose the live snapshot, including `position_ms` and `duration_ms`
  when available. They are `None` when no item is shared.
- `state` and `state_changed` carry `shared_media.state`. `reaction` carries
  `shared_media.reaction` once the reply has passed local checks. The controller
  already presents those reactions using the existing speech and subtitle output.
  `reaction_cancelled` carries the stale or interrupted `reaction_id`.
- `tier` carries `shared_media.tier` with `tier` (`L0`, `L1` or `L2`), `cue` and
  `strength`. `error` carries a reason string. `mark_credits()` and
  `set_owner_speaking(active)` accept local cues. The existing opt-in headset
  input drives owner-speech cues during a shared movie.

In the panel, the ♪ header button, the "更多" menu and the tray menu open 一起听 / 一起看
(`ui/together.py`). It explains why it is unavailable when the flag, the device session or the
`speak` grant is missing. A now-playing card sits above the conversation, and it folds to one
row for films. A local film plays in its own window. Reactions appear as small bubbles, and
cancelled ones are removed. Settings → 一起听看 turns the entry off and sets the default source.

Shared snapshots contain a sanitized title, media kind, source, playback status and
timing. Local audio contributes numeric RMS levels. Only Windows music sessions can
include an artist. A comment may include one current timed-text line with links,
domains and email addresses removed.

Tests run offscreen: `QT_QPA_PLATFORM=offscreen python -m pytest`.
`tools/render_states.py OUT_DIR` renders every panel state with placeholder text.

## Privacy rules for this repository

This repository is public from the first commit. Never commit:

- character persona text, real chat logs, user names, QQ or other account IDs
- server hostnames or IPs, SSH details, API keys, VTube Studio auth tokens, proxy credentials
- the character's Live2D model, wardrobe images, voice samples or other personal assets
- campus or location data, game save files

Configuration is loaded from local files that are git-ignored. Commit only `*.example.*` templates with placeholder values. Tests and samples use placeholder IDs and synthetic data. CI runs a secret scan on every push and pull request.

## License

AGPL-3.0-only. See [LICENSE](LICENSE).
