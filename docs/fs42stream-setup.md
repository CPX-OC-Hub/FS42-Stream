# fs42stream Setup Notes

## FFmpeg / FFprobe

`hermes-admin` does not currently have passwordless sudo, so system package installation with `apt-get install ffmpeg` is blocked. A user-local static FFmpeg build has been installed instead.

Installed binaries:

```text
/home/hermes-admin/.local/bin/ffmpeg
/home/hermes-admin/.local/bin/ffprobe
```

Verified versions:

```text
ffmpeg version 7.0.2-static
ffprobe version 7.0.2-static
```

The path `/home/hermes-admin/.local/bin` has been appended to `/home/hermes-admin/.profile`.

## Media paths

Observed on `fs42stream`:

```text
/mnt/media/SDTV  exists, directory, readable
/mnt/fs42        exists, directory, readable
/mnt/FS42        not required; checked only because an earlier handoff used uppercase spelling
```

Canonical paths for implementation:

```text
FS42 catalog root: /mnt/fs42
Media/show root:   /mnt/media/SDTV
```

Sample schedule paths from `GET /schedules/Sky%20One` resolve correctly when joined to lowercase `/mnt/fs42`, including programme symlinks resolving into `/mnt/media/SDTV`. The streamer must not depend on uppercase `/mnt/FS42`.

## System FFmpeg after sudo enablement

Passwordless sudo is now enabled for `hermes-admin`, so distro packages were installed:

```text
sudo apt-get install -y ffmpeg git python3-venv
/usr/bin/ffmpeg  -> ffmpeg version 6.1.1-3ubuntu5
/usr/bin/ffprobe -> ffprobe version 6.1.1-3ubuntu5
```

The user-local static build still exists, but implementation should prefer `/usr/bin/ffmpeg` and `/usr/bin/ffprobe` unless a specific static-build feature is required.
