# Building Decrypt OS

Build the ARM64 VM image with one command from a repo checkout:

```sh
build/build.sh --version <name> --signing-key /path/outside/repo/release-signing-ed25519.pem
```

The x86_64 image builds natively on an Arch Linux x86_64 builder, unsigned, and is
signed afterwards on the release host:

```sh
sudo build/build-x86_64.sh --version <name> --unsigned-dev      # on the x86_64 builder
build/sign_manifest.sh --dir <dir> --signing-key /path/outside/repo/release-signing-ed25519.pem
```

Everything about the builder VMs, the stages, the input cache, the outputs and how to
verify a release is in [build/README.md](build/README.md).

Run unit tests in a Linux VM, never on a machine used for live trading. Browser tests for the
Tradovate bridge never run on a Mac.
