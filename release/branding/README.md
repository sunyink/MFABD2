# Application icons

These files in `release/branding/` are build inputs. CI copies only the assets
needed by each target, never this whole directory. Desktop and Android workflows
watch `release/branding/**` for changes.

| File          | Purpose                                                                  |
| ------------- | ------------------------------------------------------------------------ |
| `app.ico`     | Windows executable: approved 16/24/32/48/64/128/256 images, 32-bit alpha |
| `title.png`   | 24px neutral pixel portrait for MFAA's shared title/window/tray icon     |
| `android.png` | 192px opaque launcher bitmap for MaaFwApp's `app.icon`                   |
| `frame.png`   | Original 1024px transparent artwork; source for derived images           |
| `app.icns`    | Approved macOS icon set, reserved for future `.app` packaging            |

`scripts/prepare_icons.py` runs after desktop package assembly. It copies
`title.png` to `install/resource/ui/title.png` and sets the generated root
`interface.json` field to `"icon": "resource/ui/title.png"` only after the copy
succeeds. `resource/ui/` holds application UI images, including future background
images; it is not registered in the task resource loading list. Background
configuration is not implemented yet. Windows additionally uses rcedit 2.0.0, verified by SHA-256, on a
temporary copy of `MFAAvalonia.exe`. The original executable is replaced only
after its Windows resource group references all seven approved images. No loose ICO or
rcedit executable is shipped. DLL files are not modified.
The source artwork, this README and unused ICNS stay in the repository. Existing
`release/mac/` script selection is unchanged; no old output directories are migrated.

MFAA currently shares one bitmap between its title, window and tray icons.
The pixel portrait therefore applies to all three, independently of the Windows
executable's multi-size icon. macOS keeps the existing flat package layout;
this change does not add a Finder application bundle or a custom ICNS Dock icon.
Linux receives the same runtime portrait, without changing desktop integration.

Android copies `android.png` into `android-build/branding/launcher.png` after
identity preparation, then changes only the build checkout's profile icon path.
The source profile continues to specify the previous `ReadMe/logo.png` fallback.
The launcher bitmap uses the original artwork on `#DFDBEB`, centered with its
visible pixels fitted inside an 82px radius on a 192px square. It is a legacy
bitmap supported by the pinned upstream, not an adaptive foreground/background
resource set. No new image-generation service runs in CI.

Each optional operation reports success or fallback in the Actions log and step
summary. A failed operation keeps the prior/default icon and does not stop the
release. If an Android build fails after icon preparation, CI restores the
original profile and retries the tests and build once. A repeated build failure,
signing failure, or APK verification failure remains fatal.

## Local checks

```sh
python scripts/verify_icons.py
```

Source/fallback tests do not establish Explorer, Dock, launcher, or device visual
acceptance. Release binaries and the relevant desktop/device must also be checked.
The test runner requires Python 3.11+; icon preparation itself supports 3.10+.
