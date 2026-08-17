# Releasing Narratarr

## The image

`ghcr.io/samdarbonne/narratarr`, built by `.github/workflows/release.yml` and pushed to
the GitHub container registry.

**The workflow needs no secret.** It signs in with the `GITHUB_TOKEN` that Actions provides
to every run. If a push fails, check the `packages: write` permission in the workflow, and
check that the package is not set to private. Do not add a registry credential.

## The tag scheme

| You do this | The registry gets |
|---|---|
| Push to `main` | `:develop` |
| Tag `v1.4.2` | `:1.4.2`, `:1.4`, `:latest` |
| Tag `v2.0.0` | `:2.0.0`, `:2.0`, `:latest`, `:2` |

**A push to `main` never moves `:latest`.** `:latest` moves only on a version tag. A person
running `:latest` in production must never receive an untagged commit, and an auto-updater
would install one within the hour.

**A major release also publishes the bare major alias.** Pin `:2` to take every later 2.x
and never cross a major boundary on your own.

`:develop` is the servarr habit: the newest `main`, for a person who wants it and accepts
the risk.

## To cut a release

```bash
git tag -a v0.2.0 -m "What changed"
git push origin v0.2.0
```

The workflow then runs the whole suite, builds the image, pushes the tags, and creates a
GitHub release with generated notes. **The push is gated on the tests.** An image that
fails its own suite never reaches the registry.

Use semver. Narratarr is pre-1.0, so the interface can still change in a minor release; say
so in the notes when it does.

## The architecture

**v1 publishes `linux/amd64` only. That is measured, not an oversight.**

The image installs torch and a spaCy transformer, and the build **runs a warmup render** to
prove the espeak fallback works. Refer to `APP-CONTRACT.md` section 11.2: without that
check, a broken fallback deletes every out-of-lexicon word from the audio in silence, and
quality control cannot see the loss.

Under QEMU emulation that warmup executes real model inference on an emulated CPU. On a
hosted runner it is far too slow to be worth it, and dropping the warmup to make the build
finish would remove the check that the step exists for.

To add `arm64` later:

1. Add `docker/setup-qemu-action@v3` before the buildx step.
2. Change `platforms:` to `linux/amd64,linux/arm64`.
3. Expect a long build, and measure it.

A native arm64 runner is the better answer.

## Docker Hub

Not configured. It needs an account and a token that the GitHub registry does not.
Add a second `docker/login-action` step and a second image name when those exist.

## Deploying a release to a server

```yaml
services:
  narratarr:
    image: ghcr.io/samdarbonne/narratarr:latest
    restart: unless-stopped
    ports: ["5164:8000"]
    cpus: 3
    mem_limit: 5g
    volumes:
      - ./config:/config
      - ./output:/output
      - ./watch:/watch
```

`mem_limit` is not decoration. Measured with the engine and the transcriber both resident:
`small.en` peaks at 2,106 MB and `distil-large-v3` at 3,394 MB. A limit means the container
dies alone instead of the kernel choosing a victim among everything else on the machine.

**Warning: do not build this image on the same machine that is running a render.** Measured
on a 4-core server: a build beside a live render took the machine off the network for 40
minutes. Pull the published image instead. That is what the registry is for.
