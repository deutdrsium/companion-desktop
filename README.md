# companion-desktop

Local desktop runtime for an AstrBot companion character:

- a procedural animation "cerebellum" that drives a Live2D model through VTube Studio
- a local voice timeline with lip-sync, subtitles and interruption
- desktop observation with explicit consent
- game executors built on the public Neuro game protocol (first target: Stardew Valley via the Neuro mod)
- the AstrBot desktop-access plugin that connects all of the above to the companion plugin

Status: early development. Work packages P0–P9 are tracked in issues/PRs.

## Privacy rules for this repository

This repository is public from the first commit. Never commit:

- character persona text, real chat logs, user names, QQ or other account IDs
- server hostnames or IPs, SSH details, API keys, VTube Studio auth tokens, proxy credentials
- the character's Live2D model, wardrobe images, voice samples or other personal assets
- campus or location data, game save files

Configuration is loaded from local files that are git-ignored. Commit only `*.example.*` templates with placeholder values. Tests and samples use placeholder IDs and synthetic data. CI runs a secret scan on every push and pull request.

## License

AGPL-3.0-only. See [LICENSE](LICENSE).
