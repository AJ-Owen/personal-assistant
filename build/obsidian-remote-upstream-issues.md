# Upstream issues in `sytone/obsidian-remote`

These three issues each reproduce on a clean
`ghcr.io/sytone/obsidian-remote:latest` and together prevent Obsidian from
rendering at all in a fresh install. Filed here as drafts for upstream PRs.

Workarounds for all three are baked into `build/Dockerfile.obsidian-remote`.

---

## Issue 1: Obsidian launches as a 200×200 window in the top-left corner

### Description

When the RDP session starts, Obsidian is launched via
`~/.config/openbox/autostart` but openbox has no application rule for it.
Obsidian's Electron window opens at its default 200×200 size in the top-left,
leaving most of the screen black/empty. Users see openbox widgets but no
usable Obsidian UI.

### Reproduce
1. `docker run --rm -p 8080:8080 ghcr.io/sytone/obsidian-remote:latest`
2. Open `http://localhost:8080`
3. Observe a tiny Obsidian window in the corner (verify with
   `xwininfo -root -tree` on display `:10`)

### Fix

Add an openbox application rule so Obsidian launches maximized. In the
Dockerfile or in `defaults/openbox/rc.xml`:

```dockerfile
RUN sed -i 's|<applications>|<applications>\n  <application class="obsidian">\n    <maximized>true</maximized>\n    <focus>yes</focus>\n    <decor>no</decor>\n  </application>|' /etc/xdg/openbox/rc.xml
```

(Alternatively, ship a custom `rc.xml` with the rule under `defaults/`.)

---

## Issue 2: Obsidian launched with both `--disable-gpu` and `--disable-software-rasterizer` — no rasterizer left

### Description

`defaults/autostart` and `defaults/menu.xml` launch Obsidian with:

```
/squashfs-root/obsidian --no-sandbox --disable-dev-shm-usage --disable-gpu --disable-software-rasterizer
```

`--disable-gpu` makes Electron fall back to the software rasterizer.
`--disable-software-rasterizer` then disables that fallback. The combination
leaves Electron with nothing capable of rendering. In the xrdp environment
(no DRI3, no GPU), this contributes to the renderer producing nothing
visible, even when Obsidian's main window is otherwise running.

### Reproduce
1. Launch the container, attach via the web UI
2. `docker exec` in: `ps aux | grep squashfs` → confirm both flags are passed
3. `xwd -root` screenshot of `:10` → all black even though `xwininfo` shows a
   sized Obsidian window

### Fix

Drop `--disable-software-rasterizer` from `defaults/autostart` and
`defaults/menu.xml`:

```
/squashfs-root/obsidian --no-sandbox --disable-dev-shm-usage --disable-gpu
```

Software rasterizer is the correct fallback inside an xrdp-only Xorg with no
GPU.

---

## Issue 3: Updated `obsidian-*.asar` in user-data-dir can become corrupt, leaving Obsidian permanently broken with `ERR_BLOCKED_BY_CLIENT`

### Description

On first run, Obsidian self-updates by downloading a newer
`obsidian-<version>.asar` into `$XDG_CONFIG_HOME/obsidian/`. If that download
is interrupted (container stopped mid-download, network hiccup, etc.), the
file is left on disk and Obsidian loads it on every subsequent start. When
the asar is corrupt, the renderer fails with:

```
electron: Failed to load URL: app://obsidian.md/index.html with error: ERR_BLOCKED_BY_CLIENT
```

The window appears, the WM marks it focused, but the renderer shows a black
screen. There is no recovery path — restarting the container does not help
because `/config/.config/obsidian/obsidian-*.asar` persists. Obsidian still
logs `App is up to date.` because hash/sig verification only runs on
download, not on load.

### Reproduce
1. Start the container. Mid-download (during the first
   `Downloading update from ...` log line), `docker compose stop`
2. Start the container again
3. Obsidian loads the partial asar → blank/black screen,
   `ERR_BLOCKED_BY_CLIENT` in stderr

### Workaround

```
rm -f /config/.config/obsidian/obsidian-*.asar
```

…then restart Obsidian. It re-downloads cleanly.

### Fix

Two reasonable upstream fixes:

1. **Verify the asar on load**, not just on download. Add a small wrapper
   script that checks the cached asar's size/hash against
   `/config/.config/obsidian/obsidian-*.asar.sig` (Obsidian writes this) and
   deletes the asar if it doesn't match before launching.
2. **Catch the renderer error** by adding `--disable-update` (or honoring an
   env var like `OBSIDIAN_DISABLE_UPDATE=1`) so users can opt out of in-place
   updates and rely on image rebuilds for new versions. The `/squashfs-root/`
   bundled asar is then always used.

A minimal launcher in `defaults/`:

```bash
#!/bin/bash
ASAR_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/obsidian"
for asar in "$ASAR_DIR"/obsidian-*.asar; do
  [ -f "$asar" ] || continue
  # asar files are zip-like; quick sanity check
  if ! head -c 4 "$asar" | grep -q '^.\{0,4\}'; then
    echo "[obsidian-remote] removing corrupt $asar"
    rm -f "$asar" "${asar}.sig" "${asar}.gz"
  fi
done
exec /squashfs-root/obsidian --no-sandbox --disable-dev-shm-usage --disable-gpu "$@"
```

(A real implementation should validate the actual asar header magic /
signature; the above is illustrative.)

---

## Suggested PR ordering

1. Issue 2 (one-line sed) — trivially correct, no behavior tradeoff.
2. Issue 1 (openbox rule) — small, isolated, makes the default UX usable.
3. Issue 3 (asar verification / opt-out) — bigger surface, deserves its own
   discussion thread before implementation.
