[English](README.md) | [简体中文](README_CN.md)

# wdnapps_video_analyzer

Video-analysis application plugin package for wdnwebui. Once installed, these 5 cards
appear on the host UI:

| Card id | Title | Description |
|---|---|---|
| `video_detection` | Audio/Video Description | Frame-by-frame VLM description across multiple streams; frame criteria, an audio-track analysis panel, and an optional `tts_alert` voice alert |
| `video_target_detection` | Video Target Monitoring | 1~32-stream grid + VLM round-robin per stream, emitting structured target events |
| `video_target_localization` | Video Arbitrary Target Localization | Specify a target in natural language → output grounding bbox (freeze-frame on hit for 2s when the event arrives) |
| `video_retrieval` | Surveillance Retro-Lookup | Search surveillance recordings for target clips with natural language |
| `audio_analysis` | Audio/Video Analysis | `hidden`, not shown in the app grid; called by the "Audio Track Analysis" panel of Audio/Video Description (`/api/audio-analysis/*`) |

## What Form It Takes

**This is a wdnwebui plugin, not a standalone application.** The card backend imports
host services with `from app.core.*` — `ComputeRegistry` (the only source of
`LLMSession`), `MultiChannelSession`, the `WdnaiDetector` base class, and so on.
These are the call paths mandated by wdnwebui design principles 4/5; a plugin cannot
route around them and should not try. Therefore this wheel must live in the **same
interpreter** as wdnwebui, with the wdnwebui repo root on `sys.path`.

## Installation

The host repo root carries an `update_rkwhl.sh` that drives the four SDKs and the two
card plugin packages together. **Prefer running it** over manual pip:

```bash
cd <wdnwebui>
./update_rkwhl.sh                       # reinstall only what the git HEAD says changed
UPDATE_RKWHL_FORCE_REBUILD=1 ./update_rkwhl.sh   # force a full reinstall
```

What the script does for this package: if HEAD is unchanged it does not touch `.venv`
at all (pip is not even run); only when HEAD changes does it run `purge_install_traces`
+ `pip install -e`. After installing it runs a card registration check
(`scripts/check_plugin_entrypoints.py`) that verifies, card by card: "the entry point
resolves → the manifest `"id"` is character-for-character identical to the entry point
name → the card directory has `static/`". This blocks the silent failure where pip
reports success all the way through, yet the host startup FATAL-skips these cards.

Manual installation also works (this is the path the script takes on first clone):

```bash
# lands in wdnwebui's venv, next to wdnai/wdnav/wdndds
cd <wdnwebui>/.venv/src
git clone git@github.com:rockchip-wardner/wdnapps_video_analyzer.git
pip install -e wdnapps_video_analyzer
```

On host restart it scans the `wdnwebui.cards` entry point group, reads each card's
`manifest.json`, and mounts its routes plus the `/static/apps/<id>` static directory.
No host code or configuration needs to change after installation.

> `pip install` does not silently skip a card for missing dependencies — the host's
> `_discover_from_entry_points()` wraps each card in its own try/except: if one card
> fails to import it only logs `LOG.error` and skips that card, the rest load normally
> and the web UI still starts. For troubleshooting, look straight for
> `[<card_id>] plugin entry point ... not importable` in the host log.

## Adding a New Card

1. Create `src/wdnapps_video_analyzer/cards/<new-card>/`, copying the layout of any
   existing card: `__init__.py` (`register_api` / `register_shutdown`), `api.py`,
   `service.py`, `manifest.json`,
   `static/{template.html,app.js,app.css,wizard.json}`.
2. Always write in-card imports in full as
   `wdnapps_video_analyzer.cards.<new-card>.xxx`; **do not** write `app.apps.*`.
3. Register in **two** places in `pyproject.toml`, both required:
   - Add `<card-id> = "…cards.<new-card>"` under
     `[project.entry-points."wdnwebui.cards"]` — the left side must be
     character-for-character identical to the `"id"` in `manifest.json`, otherwise the
     host marks it FATAL and skips it. **Use hyphens here, not underscores**: the
     package directory is `cards/video_detection/` (underscore) while the card id is
     `"video-detection"` (hyphen); writing the module name verbatim makes every
     registration fail (symptom: a stream of `[video_detection] FATAL: manifest id
     'video-detection' != entry-point name 'video_detection'` in the log, and the
     plugin silently never appears).
   - Add the card's `["manifest.json", "static/*"]` under
     `[tool.setuptools.package-data]` — without it the static assets do not enter the
     wheel, so the card is discoverable but the front end 404s.

## Boundary with the Host

A plugin may **only** `from app.core.*`; it must not import the host's `app.apps.*`
(those are other cards and may not even be installed). The reverse is forbidden too:
core must not depend on plugins — which is why, when this package was moved out,
`app/apps/video_detection/tts_alert.py` was relocated into the host's
`app/core/tools/tts_alert.py`: `app/core/tools/_builtins.py` imports it, and
`_builtins` is in turn imported by the detached `compute_worker` subprocess; once it
points at an optional plugin, the tool silently disappears when the plugin is not
installed.

## License

Apache-2.0. See [LICENSE](LICENSE).
