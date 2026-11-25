## What's New in v0.2.0

### Backup & Offline Deployment
- Selective backup and restore of images, volumes and project files.
- Model-only transfers with merge and optional mirroring.
- In-place deployment recovery using `loaf setup --all`.
- Improved terminal credential preservation and mandatory SHA-256 payload verification.
- Default gzip compression level 4.

### Open Terminal
- Chat-scoped APT package installation with persistent, isolated software environments.
- Support for offline APT repositories and runtime-editable package sources.
- Automatic terminal key generation and dedicated key setup.
- Standardized Docker resource naming and ownership labels.

### Inference & Verification
- Bundled compact CPU and CUDA 13 Ollama build recipes.
- Expanded CPU inference and authenticated Open WebUI tests.
- Dedicated disconnected release acceptance workflow for GitHub and Gitea, including account recovery, chat restoration and corrupt model blob handling.
- Hardened cleanup and terminal credential validation.

### Documentation
- Updated architecture, backup and restore documentation.
- Added local network access instructions for native Debian and WSL2.

### Breaking Changes
- Removed legacy pre-release configuration, environment, archive and terminal-server compatibility fallbacks.
- Deployment archive restoration now requires a valid SHA-256 payload manifest.
- Updated Open Terminal Docker ownership labels and resource naming.
